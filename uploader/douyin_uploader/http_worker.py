#!/usr/bin/env python
"""DouYin_Spider 外部 venv 内的 worker。

父进程把本文件复制到私有临时目录，用外部 venv 的 Python 启动，通过
stdin/stdout 的 JSON lines 通信。worker 只依赖第三方源码和其 requirements，
不 import 本仓库的任何模块。

协议：
- 父进程 → worker：``{"cmd": "submit"}``（只在 create_v2 请求已就绪时）
- worker → 父进程：
  - ``{"event": "qrcode", "url": "..."}``
  - ``{"event": "ready_to_submit"}``
  - ``{"event": "result", ...}``
  - ``{"event": "error", "stage": ..., "message": ...}``

create_v2 的提交握手发生在模块级 ``_request`` 上：第三方代码完成上传、CSRF、
加密头等所有前置步骤、真正要发 POST 时才通知父进程；父进程回 submit 之前
请求不会发出。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
CREATE_V2_PATH = "/web/api/media/aweme/create_v2/"
_VERIFY_PASS_VALUES = frozenset({"", "0", "pass", "passed", "none"})


def _emit(protocol, event: dict) -> None:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    protocol.write(payload + "\n")
    protocol.flush()


def _read_command():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return {"cmd": line}
    return payload if isinstance(payload, dict) else {"cmd": str(payload)}


def _load_task(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("task JSON 必须是对象")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(
            f"协议版本不匹配：{payload.get('protocol_version')!r} != {PROTOCOL_VERSION}"
        )
    return payload


def _insert_runtime_src(task: dict) -> None:
    src = Path(str(task.get("runtime_src") or "")).expanduser()
    if not src.is_dir():
        raise RuntimeError(f"外部运行时源码目录不存在：{src}")
    sys.path.insert(0, str(src))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_credential_fields(payload: dict) -> dict:
    missing = [
        key
        for key in ("cookie", "ticket", "ts_sign", "private_key")
        if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(f"HTTP 凭据缺少字段：{', '.join(missing)}")
    return payload


def _load_credentials(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("HTTP 凭据必须是 JSON 对象")
    return _require_credential_fields(payload)


def _build_auth(credentials: dict):
    from builder.auth import DouyinAuth

    return DouyinAuth.from_cookie(
        credentials["cookie"],
        ticket=credentials.get("ticket"),
        ts_sign=credentials.get("ts_sign"),
        client_cert=credentials.get("client_cert"),
        private_key=credentials.get("private_key"),
        dtrait_blob=credentials.get("dtrait_blob"),
        bootstrap_creator=True,
    )


def _write_credentials(path: str | Path, auth) -> dict:
    payload = {
        "schema_version": 1,
        "cookie": auth.cookie_str,
        "ticket": auth.ticket,
        "ts_sign": auth.ts_sign,
        "client_cert": auth.client_cert,
        "private_key": auth.private_key,
        "dtrait_blob": auth.dtrait_blob,
        "saved_at": _now(),
    }
    _require_credential_fields(payload)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
        file_obj.write("\n")
        file_obj.flush()
        os.fsync(file_obj.fileno())
    os.replace(temp_path, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return payload


class SubmitGate:
    """拦截 create_v2 的 POST，先与父进程握手，再真正发送。"""

    def __init__(self, protocol) -> None:
        self.protocol = protocol
        self.create_started = False
        self.submit_sent = False
        self.response_received = False
        self.http_status: int | None = None
        self.verify_decision: str | None = None
        self.transport_error: str | None = None

    def install(self):
        from dy_apis import douyin_creator_api as api

        original = api._request
        gate = self

        def _wrapped(auth, method, url, **kwargs):
            is_create = (
                str(method).upper() == "POST"
                and CREATE_V2_PATH in str(url)
            )
            if not is_create:
                return original(auth, method, url, **kwargs)

            if gate.create_started:
                raise RuntimeError("create_v2 被重复调用，已阻止第二次提交")
            gate.create_started = True
            _emit(gate.protocol, {"event": "ready_to_submit"})

            command = _read_command()
            if not command or str(command.get("cmd") or "").lower() != "submit":
                gate.transport_error = "父进程未确认提交（未收到 submit 命令）"
                raise RuntimeError(
                    "未收到 submit 确认，create_v2 请求未发送"
                )
            gate.submit_sent = True
            try:
                response = original(auth, method, url, **kwargs)
            except BaseException as exc:
                gate.transport_error = f"{type(exc).__name__}: {exc}"
                raise

            gate.response_received = True
            try:
                gate.http_status = int(response.status_code)
            except Exception:
                gate.http_status = None
            try:
                decision = response.headers.get("x-tt-verify-passport-decision")
            except Exception:
                decision = None
            if decision:
                gate.verify_decision = str(decision)
            return response

        api._request = _wrapped
        return api


def _needs_human(decision: str | None) -> bool:
    if not decision:
        return False
    return decision.strip().lower() not in _VERIFY_PASS_VALUES


def _publish_result(gate: SubmitGate, success, message, res_json) -> dict:
    item_id = None
    if isinstance(res_json, dict):
        item_id = res_json.get("item_id")
    result: dict[str, Any] = {
        "event": "result",
        "mode": "publish",
        "message": str(message or ""),
        "http_status": gate.http_status,
        "create_started": gate.create_started,
        "submit_sent": gate.submit_sent,
        "response_received": gate.response_received,
        "item_id": str(item_id) if item_id else None,
    }
    if success and item_id:
        result["success"] = True
        return result

    result["success"] = False
    if not gate.create_started:
        result["stage"] = "pre_submit"
        return result

    if _needs_human(gate.verify_decision):
        result["stage"] = "submit"
        result["needs_human"] = True
        result["verify_decision"] = gate.verify_decision
        return result

    status_code = res_json.get("status_code") if isinstance(res_json, dict) else None
    if isinstance(res_json, dict) and status_code not in (0, None):
        result["stage"] = "submit"
        result["rejected"] = True
        result["status_code"] = status_code
        return result

    if not gate.response_received:
        result["stage"] = "submit_unknown"
        if gate.transport_error:
            result["transport_error"] = gate.transport_error
        return result

    if gate.http_status is not None and 400 <= gate.http_status < 500:
        result["stage"] = "submit"
        result["rejected"] = True
        return result

    # 服务端有响应但既不是成功也不是可判定的拒绝（例如 5xx、200 非 JSON），
    # 保守地按“提交结果未知”处理，避免重复发布。
    result["stage"] = "submit_unknown"
    return result


def _run_self_test(task: dict, protocol) -> int:
    from builder.auth import DouyinAuth
    from dy_apis import douyin_creator_api
    from dy_apis.douyin_creator_api import DouyinCreatorAPI

    assert callable(DouyinAuth.from_cookie)
    assert callable(DouyinAuth.from_qrcode_login)
    assert callable(DouyinCreatorAPI.post_video)
    assert callable(douyin_creator_api._request)
    _emit(
        protocol,
        {
            "event": "result",
            "mode": "self-test",
            "success": True,
            "message": "SELF_TEST_OK",
        },
    )
    return 0


def _run_login(task: dict, protocol) -> int:
    from builder.auth import DouyinAuth

    timeout = float(task.get("timeout") or 300)
    credentials_path = task.get("credentials_path")
    if not credentials_path:
        raise ValueError("login 任务缺少 credentials_path")

    def _on_qrcode(url: str) -> None:
        _emit(protocol, {"event": "qrcode", "url": url})

    auth = None
    try:
        auth = DouyinAuth.from_qrcode_login(
            timeout=timeout,
            show_qr=False,
            on_qrcode=_on_qrcode,
            bootstrap_creator=True,
        )
        payload = _write_credentials(credentials_path, auth)
        _emit(
            protocol,
            {
                "event": "result",
                "mode": "login",
                "success": True,
                "message": "login completed",
                "cookie_length": len(payload["cookie"]),
                "has_client_cert": bool(payload["client_cert"]),
                "has_dtrait_blob": bool(payload["dtrait_blob"]),
            },
        )
        return 0
    finally:
        if auth is not None:
            try:
                auth.close()
            except Exception:
                pass


def _run_check(task: dict, protocol) -> int:
    from dy_apis.douyin_creator_api import DouyinCreatorAPI

    credentials_path = task.get("credentials_path")
    if not credentials_path:
        raise ValueError("check 任务缺少 credentials_path")
    auth = _build_auth(_load_credentials(credentials_path))
    try:
        preview = DouyinCreatorAPI.get_preview_video_list(auth)
        valid = isinstance(preview, list)
        _emit(
            protocol,
            {
                "event": "result",
                "mode": "check",
                "success": bool(valid),
                "valid": bool(valid),
                "message": "creator read ok" if valid else "unexpected work_list response",
            },
        )
        return 0 if valid else 1
    finally:
        try:
            auth.close()
        except Exception:
            pass


def _run_publish(task: dict, protocol) -> int:
    from dy_apis.douyin_creator_api import DouyinCreatorAPI

    credentials_path = task.get("credentials_path")
    if not credentials_path:
        raise ValueError("publish 任务缺少 credentials_path")
    video = task.get("video")
    if not video:
        raise ValueError("publish 任务缺少 video")
    if not Path(video).is_file():
        raise FileNotFoundError(f"视频文件不存在：{video}")

    auth = _build_auth(_load_credentials(credentials_path))
    gate = SubmitGate(protocol)
    gate.install()
    try:
        try:
            success, message, res_json = DouyinCreatorAPI.post_video(
                auth,
                video=video,
                title=task.get("title") or "",
                desc=task.get("desc") or "",
                visibility=int(task.get("visibility", 0)),
                allow_download=bool(task.get("allow_download", True)),
                timing=task.get("timing"),
                cover=task.get("cover"),
            )
        except Exception as exc:
            # post_video 本身很少抛出；一旦抛出，用 gate 状态区分“提交前失败”
            # 与“提交后结果未知”，不能一律按未知处理。
            result = _publish_result(
                gate, False, f"{type(exc).__name__}: {exc}", None
            )
            _emit(protocol, result)
            return 1
        result = _publish_result(gate, success, message, res_json)
        _emit(protocol, result)
        return 0 if result.get("success") else 1
    finally:
        try:
            auth.close()
        except Exception:
            pass


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: http_worker.py <task.json>", file=sys.stderr)
        return 2

    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass

    # 协议通道必须是原始 stdout；第三方代码的 print/log 全部改走 stderr。
    protocol = sys.stdout
    sys.stdout = sys.stderr

    stage = "init"
    try:
        task = _load_task(argv[1])
        _insert_runtime_src(task)
        mode = str(task.get("mode") or "")
        if mode == "self-test":
            return _run_self_test(task, protocol)
        if mode == "login":
            stage = "login"
            return _run_login(task, protocol)
        if mode == "check":
            stage = "check"
            return _run_check(task, protocol)
        if mode == "publish":
            stage = "pre_submit"
            return _run_publish(task, protocol)
        raise ValueError(f"未知 worker 模式：{mode!r}")
    except Exception as exc:
        _emit(
            protocol,
            {
                "event": "error",
                "stage": stage,
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=20),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
