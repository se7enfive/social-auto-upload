"""抖音纯 HTTP 发布引擎：外部 worker 进程、提交握手与状态映射。

浏览器引擎仍在 ``uploader/douyin_uploader/main.py``；本模块只服务
``--engine http``，不启动任何浏览器。发布状态机复用
``utils/publish_safety.PublicationAttempt`` / ``submit_once``。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sau_commands.arguments import resolve_http_account_file
from utils.publish_safety import (
    PublishNeedsHuman,
    PublishOutcomeUnknown,
    PublishRejected,
    state_root,
    submit_once,
)
from uploader.douyin_uploader.http_runtime import (
    DOUYIN_SPIDER_COMMIT,
    DouyinHttpRuntimeError,
    clean_external_env,
    is_runtime_installed,
    runtime_dir,
    validate_runtime,
    venv_python,
)

ENGINE_AUTO = "auto"
ENGINE_BROWSER = "browser"
ENGINE_HTTP = "http"
ENGINES = (ENGINE_AUTO, ENGINE_BROWSER, ENGINE_HTTP)

VISIBILITY_VALUES = {"public": 0, "private": 1, "friends": 2}

PROTOCOL_VERSION = 1
DEFAULT_LOGIN_TIMEOUT = 300.0
DEFAULT_CHECK_TIMEOUT = 120.0
_LOGIN_GRACE = 120.0
_MAX_TITLE_UTF16_UNITS = 20

_SECRET_RE = re.compile(
    r"(?i)\b(cookie|ticket|ts_sign|private_key|client_cert)([\"']?\s*[:=]\s*[\"']?)"
    r"([^\"',;\s}]+)"
)


class DouyinHttpError(RuntimeError):
    """HTTP 引擎基础异常。"""


class DouyinHttpPreflightError(DouyinHttpError):
    """提交前即可判定的配置/参数错误；可安全重试。"""


class DouyinHttpWorkerError(DouyinHttpError):
    """worker 进程协议或生命周期错误。"""


class DouyinHttpWorkerExited(DouyinHttpWorkerError):
    """worker 在返回结果前退出。"""


@dataclass(slots=True)
class HttpLoginResult:
    account_file: Path
    reused: bool = False


@dataclass(slots=True)
class HttpImportResult:
    account_file: Path
    valid: bool


class DouyinHttpCredentialImportError(DouyinHttpPreflightError):
    """浏览器登录态无法安全转换为 HTTP 凭据。"""


# Playwright storage_state 中 Chrome 写入的 Cookie 顺序；同名重复项保留最后值。
_STORAGE_COOKIE_ORDER = (
    "bd_ticket_guard_client_web_domain", "n_mh", "has_biz_token",
    "__security_server_data_status", "_bd_ticket_crypt_doamin",
    "SEARCH_RESULT_LIST_TYPE", "live_use_vvc", "store-region",
    "store-region-src", "d_ticket", "is_staff_user", "enter_pc_once",
    "hevc_supported", "passport_csrf_token", "passport_csrf_token_default",
    "UIFID", "uid_tt", "uid_tt_ss", "sid_tt", "sessionid", "sessionid_ss",
    "bd_ticket_guard_ts_sign_id", "__security_mc_1_s_sdk_crypt_sdk",
    "__security_mc_1_s_sdk_cert_key",
    "__security_mc_1_s_sdk_sign_data_key_web_protect", "is_dbsc",
    "is_support_rtm_web_ts", "stream_recommend_feed_params",
    "home_can_add_dy_2_desktop", "strategyABtestKey",
    "publish_badge_show_info", "is_dash_user", "sid_guard",
    "session_tlb_tag", "sid_ucp_v1", "ssid_ucp_v1", "IsDouyinActive",
    "x-web-secsdk-uid", "ttwid", "csrf_session_id", "s_v_web_id",
    "odin_tt", "x_tt_token", "fpk1", "fpk2",
)
_SDK_TICKET_KEY = "security-sdk/s_sdk_sign_data_key/web_protect"
_SDK_PRIVATE_KEY_KEY = "security-sdk/s_sdk_crypt_sdk"


def _decode_sdk_entry(raw: str) -> dict:
    """解开 localStorage 的 ``{"data": "<json string>"}`` 包装。"""
    try:
        outer = json.loads(raw)
        inner = json.loads(outer["data"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DouyinHttpCredentialImportError(
            "浏览器安全 SDK 凭据格式无法识别"
        ) from exc
    if not isinstance(inner, dict):
        raise DouyinHttpCredentialImportError("浏览器安全 SDK 凭据不是 JSON 对象")
    return inner


def _storage_sdk_material(data: dict) -> dict:
    """从同一次导出的 storage_state 提取票据；不读取文件之外的数据。"""
    for origin in data.get("origins") or []:
        if str(origin.get("origin") or "") != "https://creator.douyin.com":
            continue
        entries = {
            str(entry.get("name") or ""): str(entry.get("value") or "")
            for entry in origin.get("localStorage") or []
        }
        ticket_raw = entries.get(_SDK_TICKET_KEY)
        if not ticket_raw:
            break
        material = _decode_sdk_entry(ticket_raw)
        keys_raw = entries.get(_SDK_PRIVATE_KEY_KEY)
        if keys_raw:
            private_key = _decode_sdk_entry(keys_raw).get("ec_privateKey")
            if private_key:
                material["private_key"] = private_key
        return material
    raise DouyinHttpCredentialImportError(
        "storage_state 缺少 creator.douyin.com 的安全 SDK 票据；"
        "请先用浏览器引擎登录并导出同一账号的完整登录态"
    )


def _storage_cookie_string(data: dict, ticket_material: dict) -> str:
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        raise DouyinHttpCredentialImportError("storage_state 缺少 cookies 列表")
    now = time.time()
    pairs: dict[str, str] = {}
    order: list[str] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lower().lstrip(".")
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name or not value:
            continue
        if name not in pairs:
            order.append(name)
        pairs[name] = value
    if "sessionid" not in pairs:
        raise DouyinHttpCredentialImportError("storage_state 中没有登录态 sessionid")
    ts_sign = str(ticket_material.get("ts_sign") or "").strip()
    if ts_sign:
        if "bd_ticket_guard_ts_sign_id" not in pairs:
            order.append("bd_ticket_guard_ts_sign_id")
        pairs["bd_ticket_guard_ts_sign_id"] = ts_sign
    ordered = [name for name in _STORAGE_COOKIE_ORDER if name in pairs]
    ordered.extend(name for name in order if name not in ordered)
    return "; ".join(f"{name}={pairs[name]}" for name in ordered)


def build_http_credentials_from_storage_state(storage_path: Path) -> dict:
    """把本机已登录 Chrome 的 storage_state 转成 HTTP worker 凭据。"""
    path = Path(storage_path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DouyinHttpCredentialImportError(
            f"无法读取浏览器 storage_state：{path}"
        ) from exc
    if not isinstance(data, dict):
        raise DouyinHttpCredentialImportError("storage_state 必须是 JSON 对象")
    material = _storage_sdk_material(data)
    payload = {
        "schema_version": 1,
        "cookie": _storage_cookie_string(data, material),
        "ticket": material.get("ticket"),
        "ts_sign": material.get("ts_sign"),
        "client_cert": material.get("client_cert"),
        "private_key": material.get("private_key"),
        "dtrait_blob": material.get("dtrait_blob"),
        "saved_at": datetime.now().astimezone().isoformat(),
        "source": "browser-storage-state",
    }
    missing = [
        key
        for key in ("cookie", "ticket", "ts_sign", "private_key")
        if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise DouyinHttpCredentialImportError(
            f"浏览器 storage_state 缺少 HTTP 发布所需字段：{', '.join(missing)}"
        )
    payload["platform"] = "douyin"
    payload["engine"] = ENGINE_HTTP
    return payload


def redact_secrets(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2***", str(text or ""))


def _browser_only_options(request) -> bool:
    return bool(
        getattr(request, "prepare_only", False)
        or getattr(request, "declaration", None)
        or getattr(request, "product_link", "")
        or getattr(request, "product_title", "")
        or getattr(request, "collection_name", None)
    )


def resolve_upload_engine(request) -> str:
    """无副作用地选择引擎；auto 不访问网络、不创建目录。"""
    requested = str(getattr(request, "engine", ENGINE_BROWSER) or ENGINE_BROWSER).strip().lower()
    if requested not in ENGINES:
        raise DouyinHttpPreflightError(
            f"未知抖音上传引擎 {requested!r}，可选 auto/browser/http"
        )
    visibility = str(
        getattr(request, "visibility", "public") or "public"
    ).strip().lower()
    if visibility not in VISIBILITY_VALUES:
        raise DouyinHttpPreflightError(
            f"未知可见范围 {visibility!r}，可选 public/private/friends"
        )
    if visibility != "public":
        # 浏览器路径只会公开发布；非公开范围必须走 HTTP，绝不能静默降级。
        if _browser_only_options(request):
            raise DouyinHttpPreflightError(
                "非公开可见范围只能由 HTTP 引擎处理，但当前选项需要浏览器引擎"
                "（--prepare-only / --declaration / 商品 / 合集）；"
                "请改用 --visibility public，或去掉这些选项后重试"
            )
        if requested == ENGINE_BROWSER:
            raise DouyinHttpPreflightError(
                "浏览器引擎只会公开发布，不能使用 --visibility private/friends；"
                "请改用 --engine http，或改为 --visibility public"
            )
        return ENGINE_HTTP
    if requested != ENGINE_AUTO:
        return requested
    if _browser_only_options(request):
        return ENGINE_BROWSER
    if getattr(request, "skip_ai_declaration", False):
        return ENGINE_HTTP
    account_file = resolve_http_account_file("douyin", request.account_name)
    if is_runtime_installed() and account_file.is_file():
        return ENGINE_HTTP
    return ENGINE_BROWSER


def _utf16_units(text: str) -> int:
    return len(str(text).encode("utf-16-le")) // 2


def _select_cover(request) -> Path | None:
    for candidate in (
        getattr(request, "thumbnail_portrait_file", None),
        getattr(request, "thumbnail_file", None),
        getattr(request, "thumbnail_landscape_file", None),
    ):
        if candidate:
            return Path(candidate)
    return None


def _schedule_timestamp(publish_date) -> float | None:
    if isinstance(publish_date, datetime):
        return publish_date.timestamp()
    if isinstance(publish_date, (int, float)) and publish_date:
        return float(publish_date)
    return None


def _visibility_value(request) -> int:
    raw = str(getattr(request, "visibility", "public") or "public").strip().lower()
    if raw not in VISIBILITY_VALUES:
        raise DouyinHttpPreflightError(
            f"未知可见范围 {raw!r}，可选 public/private/friends"
        )
    return VISIBILITY_VALUES[raw]


def _build_description(description: str, tags: list[str]) -> str:
    parts = []
    if str(description or "").strip():
        parts.append(str(description).strip())
    parts.extend(f"#{tag}" for tag in tags if tag)
    return " ".join(parts)


def _require_file(path: Path | None, label: str) -> Path:
    if path is None:
        raise DouyinHttpPreflightError(f"HTTP 引擎必须提供{label}")
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise DouyinHttpPreflightError(f"{label}不存在：{resolved}")
    try:
        if resolved.stat().st_size <= 0:
            raise DouyinHttpPreflightError(f"{label}为空文件：{resolved}")
    except OSError as exc:
        raise DouyinHttpPreflightError(f"无法读取{label}：{resolved}: {exc}") from exc
    return resolved


def _load_http_credentials(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DouyinHttpPreflightError(
            f"HTTP 凭据无法读取：{path}。请重新运行 "
            f"`sau douyin login --engine http`"
        ) from exc
    if not isinstance(payload, dict):
        raise DouyinHttpPreflightError(f"HTTP 凭据格式错误：{path}")
    missing = [
        key
        for key in ("cookie", "ticket", "ts_sign", "private_key")
        if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise DouyinHttpPreflightError(
            f"HTTP 凭据缺少字段 {', '.join(missing)}：{path}。请重新扫码登录"
        )
    return payload


def validate_http_request(request) -> tuple[Path, Path, Path, int, float | None]:
    """HTTP 发布预检；返回 (runtime, credentials, cover, visibility, timing)。

    只在 ``attempt.begin()`` 之后调用，失败会落到 ``failed_before_submit``。
    """
    try:
        runtime = validate_runtime()
    except DouyinHttpRuntimeError as exc:
        raise DouyinHttpPreflightError(
            f"{exc}。请先运行 `sau douyin setup --accept-third-party`"
        ) from exc

    if getattr(request, "prepare_only", False):
        raise DouyinHttpPreflightError(
            "HTTP 引擎没有草稿接口，不能使用 --prepare-only；"
            "请去掉该参数直接发布，或改用 --engine browser 存草稿"
        )
    if getattr(request, "declaration", None):
        raise DouyinHttpPreflightError(
            "HTTP 引擎无法写入抖音自主声明（--declaration），请改用浏览器引擎"
        )
    if getattr(request, "product_link", "") or getattr(request, "product_title", ""):
        raise DouyinHttpPreflightError("HTTP 引擎暂不支持商品链接")
    if getattr(request, "collection_name", None):
        raise DouyinHttpPreflightError("HTTP 引擎暂不支持合集")
    if not getattr(request, "skip_ai_declaration", False):
        raise DouyinHttpPreflightError(
            "HTTP 引擎当前无法写入“内容由AI生成”声明；"
            "确认接受无声明发布后，请显式加 --skip-ai-declaration"
        )

    title = str(getattr(request, "title", "") or "")
    if not title.strip():
        raise DouyinHttpPreflightError("标题不能为空")
    if _utf16_units(title) > _MAX_TITLE_UTF16_UNITS:
        raise DouyinHttpPreflightError(
            f"标题超过抖音 {_MAX_TITLE_UTF16_UNITS} 字上限：{title!r}"
        )

    _require_file(Path(request.video_file), "视频文件")
    cover = _require_file(_select_cover(request), "封面（--thumbnail / --thumbnail-portrait / --thumbnail-landscape）")
    visibility = _visibility_value(request)

    timing = _schedule_timestamp(request.publish_date)
    if timing is not None and timing <= time.time():
        raise DouyinHttpPreflightError("定时发布时间已过去，拒绝发布")

    credentials_path = resolve_http_account_file("douyin", request.account_name)
    if not credentials_path.is_file():
        raise DouyinHttpPreflightError(
            f"HTTP 凭据不存在：{credentials_path}。请先运行 "
            f"`sau douyin login --account {request.account_name} --engine http`"
        )
    _load_http_credentials(credentials_path)
    return runtime, credentials_path, cover, visibility, timing


def _build_publish_task(
    request,
    *,
    runtime: Path,
    credentials_path: Path,
    cover: Path,
    visibility: int,
    timing: float | None,
) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "mode": "publish",
        "runtime_src": str(runtime / "src"),
        "video": str(Path(request.video_file).expanduser().resolve()),
        "title": str(request.title),
        "desc": _build_description(request.description, request.tags),
        "cover": str(cover.resolve()),
        "visibility": visibility,
        "timing": timing,
        "allow_download": True,
        "credentials_path": str(credentials_path.resolve()),
    }


class HttpWorkerProcess:
    """外部 venv 的 worker 子进程与 JSON-lines 协议。"""

    def __init__(
        self,
        runtime: Path,
        task: dict,
        *,
        worker_source: Path | None = None,
        python_executable: Path | None = None,
    ) -> None:
        self.runtime = Path(runtime)
        self.task = task
        self.worker_source = (
            Path(worker_source)
            if worker_source is not None
            else Path(__file__).with_name("http_worker.py")
        )
        self.python_executable = (
            Path(python_executable)
            if python_executable is not None
            else venv_python(self.runtime)
        )
        self.final_result: dict | None = None
        self.last_verify_decision: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._temp_dir: Path | None = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._closed = False

    async def start(self) -> None:
        if self._proc is not None:
            raise DouyinHttpWorkerError("worker 已启动")
        self._temp_dir = Path(tempfile.mkdtemp(prefix="sau-douyin-http-"))
        worker_copy = self._temp_dir / "http_worker.py"
        task_path = self._temp_dir / "task.json"
        try:
            shutil.copy2(self.worker_source, worker_copy)
            task_path.write_text(
                json.dumps(self.task, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._proc = await asyncio.create_subprocess_exec(
                str(self.python_executable),
                str(worker_copy),
                str(task_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._temp_dir),
                env=clean_external_env(),
            )
        except Exception:
            self._cleanup_temp()
            raise
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                self._push_stderr(f"worker 非 JSON 输出: {text[:500]}")
                await self._queue.put(
                    {
                        "event": "error",
                        "stage": "protocol",
                        "message": f"worker 输出不是 JSON: {text[:500]}",
                    }
                )
                continue
            if isinstance(event, dict):
                await self._queue.put(event)

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            self._push_stderr(line.decode("utf-8", "replace").rstrip())

    def _push_stderr(self, line: str) -> None:
        self._stderr_lines.append(line[:2000])
        if len(self._stderr_lines) > 200:
            del self._stderr_lines[:-200]

    def stderr_tail(self, limit: int = 40) -> str:
        return redact_secrets("\n".join(self._stderr_lines[-limit:]))

    async def next_event(self, timeout: float) -> dict:
        if self._proc is None:
            raise DouyinHttpWorkerError("worker 尚未启动")
        if timeout <= 0:
            raise asyncio.TimeoutError("等待 worker 事件超时（发布预算已耗尽）")

        event_task = asyncio.create_task(self._queue.get())
        exit_task = asyncio.create_task(self._proc.wait())
        done, pending = await asyncio.wait(
            {event_task, exit_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if event_task in done:
            return event_task.result()
        if exit_task in done:
            await self._drain_after_exit()
            try:
                return self._queue.get_nowait()
            except asyncio.QueueEmpty:
                code = self._proc.returncode
                tail = self.stderr_tail()
                raise DouyinHttpWorkerExited(
                    f"worker 在返回结果前退出（exit={code}）。"
                    + (f" stderr 尾部:\n{tail}" if tail else "")
                )
        raise asyncio.TimeoutError("等待 worker 事件超时")

    async def _drain_after_exit(self) -> None:
        if self._stdout_task is not None and not self._stdout_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._stdout_task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass
        await asyncio.sleep(0)

    async def send_submit(self) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DouyinHttpWorkerError("worker stdin 不可用")
        if self._proc.returncode is not None:
            raise DouyinHttpWorkerExited(
                f"worker 已退出（exit={self._proc.returncode}），无法提交"
            )
        self._proc.stdin.write(b'{"cmd":"submit"}\n')
        await self._proc.stdin.drain()

    async def wait_ready_or_error(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            event = await self.next_event(remaining)
            kind = event.get("event")
            if kind == "ready_to_submit":
                return
            if kind == "error":
                raise DouyinHttpPreflightError(
                    f"worker 提交前失败（{event.get('stage')}）：{event.get('message')}"
                    + self._tail_suffix()
                )
            if kind == "result":
                raise DouyinHttpPreflightError(
                    f"worker 在提交前提前返回：{event.get('message') or event}"
                )
            # qrcode 等事件在 publish 模式不会出现；忽略。

    def _tail_suffix(self) -> str:
        tail = self.stderr_tail(12)
        return f"\nstderr 尾部:\n{tail}" if tail else ""

    async def wait_final(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            event = await self.next_event(remaining)
            kind = event.get("event")
            if kind == "result":
                if event.get("mode") != "publish":
                    raise DouyinHttpWorkerError(
                        f"worker 返回了错误模式的 result：{event.get('mode')!r}"
                    )
                self.final_result = event
                if event.get("success"):
                    return
                message = str(event.get("message") or "未说明原因")
                verify = event.get("verify_decision")
                if verify:
                    self.last_verify_decision = str(verify)
                if event.get("needs_human"):
                    raise PublishNeedsHuman(
                        f"抖音要求二次验证，提交未确认：{message}"
                    )
                if event.get("rejected"):
                    raise PublishRejected(f"抖音明确拒绝发布：{message}")
                raise PublishOutcomeUnknown(
                    f"提交已发出但结果未确认（stage={event.get('stage')}）：{message}"
                )
            if kind == "error":
                raise PublishOutcomeUnknown(
                    f"提交后 worker 报错（{event.get('stage')}）：{event.get('message')}"
                    + self._tail_suffix()
                )
            # qrcode 等事件忽略。

    async def wait_result(self, timeout: float, *, on_qrcode=None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            event = await self.next_event(remaining)
            kind = event.get("event")
            if kind == "qrcode":
                if on_qrcode is not None:
                    on_qrcode(str(event.get("url") or ""))
                continue
            if kind == "result":
                return event
            if kind == "error":
                raise DouyinHttpWorkerError(
                    f"worker 失败（{event.get('stage')}）：{event.get('message')}"
                    + self._tail_suffix()
                )
            if kind == "ready_to_submit":
                raise DouyinHttpWorkerError("worker 在非发布模式请求提交")

    async def risk_signal(self) -> str | None:
        if self.last_verify_decision:
            return f"x-tt-verify-passport-decision={self.last_verify_decision}"
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            if proc.returncode is None:
                await self._terminate(proc)
            try:
                await proc.wait()
            except Exception:
                pass
        tasks = [
            task
            for task in (self._stdout_task, self._stderr_task)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_temp()

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(os.getpgid(proc.pid), 15)  # type: ignore[attr-defined]
        except Exception:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _cleanup_temp(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()


async def upload_video_http(request, attempt) -> dict:
    """在已有 PublicationAttempt 内执行 HTTP 发布；不创建第二个锁/attempt。"""
    runtime, credentials_path, cover, visibility, timing = validate_http_request(request)
    task = _build_publish_task(
        request,
        runtime=runtime,
        credentials_path=credentials_path,
        cover=cover,
        visibility=visibility,
        timing=timing,
    )
    evidence = {
        "engine": ENGINE_HTTP,
        "title": str(request.title),
        "visibility": str(request.visibility),
        "ai_declaration": "skipped",
        "cover": str(cover),
        "tags_count": len(request.tags),
    }
    worker = HttpWorkerProcess(runtime, task)
    try:
        await worker.start()
        await worker.wait_ready_or_error(attempt.deadline.remaining())
        await submit_once(
            safety=attempt,
            click=worker.send_submit,
            wait_success=worker.wait_final,
            detect_risk=worker.risk_signal,
            evidence=evidence,
        )
        result = worker.final_result or {}
        if result.get("item_id"):
            attempt.mark_submitted(
                {
                    **evidence,
                    "item_id": result.get("item_id"),
                    "http_status": result.get("http_status"),
                }
            )
        return result
    finally:
        await worker.aclose()


async def check_douyin_http(
    account_name: str, *, credentials_path: Path | None = None
) -> bool:
    try:
        runtime = validate_runtime()
    except DouyinHttpRuntimeError as exc:
        raise DouyinHttpError(
            f"{exc}。请先运行 `sau douyin setup --accept-third-party`"
        ) from exc
    credentials_path = Path(credentials_path) if credentials_path else resolve_http_account_file(
        "douyin", account_name
    )
    if not credentials_path.is_file():
        raise DouyinHttpError(
            f"HTTP 凭据不存在：{credentials_path}。请先运行 "
            f"`sau douyin login --account {account_name} --engine http`"
        )
    task = {
        "protocol_version": PROTOCOL_VERSION,
        "mode": "check",
        "runtime_src": str(runtime / "src"),
        "credentials_path": str(credentials_path.resolve()),
    }
    worker = HttpWorkerProcess(runtime, task)
    try:
        await worker.start()
        result = await worker.wait_result(DEFAULT_CHECK_TIMEOUT)
        return bool(result.get("valid") and result.get("success", True))
    except (DouyinHttpWorkerError, asyncio.TimeoutError):
        return False
    finally:
        await worker.aclose()


def _show_login_qr(account_name: str, url: str) -> None:
    if not url:
        return
    try:
        import segno

        qr_code = segno.make(url)
        print("请用抖音 App 扫码登录（HTTP 引擎）：", flush=True)
        qr_code.terminal(compact=True)
        qr_path = state_root() / "qrcodes" / f"douyin-http-{account_name}.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_code.save(str(qr_path))
        print(f"二维码已保存：{qr_path}", flush=True)
    except Exception:
        print(f"二维码链接：{url}", flush=True)


def _remove_login_qr(account_name: str) -> None:
    try:
        (state_root() / "qrcodes" / f"douyin-http-{account_name}.png").unlink(
            missing_ok=True
        )
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
        file_obj.write("\n")
        file_obj.flush()
        os.fsync(file_obj.fileno())
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


async def import_http_credentials_from_browser(
    account_name: str, storage_path: Path
) -> HttpImportResult:
    """导入已登录 Chrome 的凭据，先只读校验候选文件再原子替换。"""
    payload = build_http_credentials_from_storage_state(storage_path)
    payload["account"] = account_name
    account_file = resolve_http_account_file("douyin", account_name)
    account_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{account_file.name}.", suffix=".import", dir=account_file.parent
    )
    os.close(fd)
    staging = Path(temp_name)
    try:
        _write_private_json(staging, payload)
        valid = await check_douyin_http(account_name, credentials_path=staging)
        if valid:
            os.replace(staging, account_file)
            try:
                os.chmod(account_file, 0o600)
            except OSError:
                pass
        return HttpImportResult(account_file=account_file, valid=valid)
    finally:
        staging.unlink(missing_ok=True)


async def login_douyin_http(
    account_name: str,
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    force: bool = False,
    on_qrcode=None,
) -> HttpLoginResult:
    """扫码登录并保存 HTTP 票据；默认先复用已校验的凭据，除非 --force。"""
    try:
        runtime = validate_runtime()
    except DouyinHttpRuntimeError as exc:
        raise DouyinHttpError(
            f"{exc}。请先运行 `sau douyin setup --accept-third-party`"
        ) from exc

    account_file = resolve_http_account_file("douyin", account_name)
    if not force and account_file.is_file():
        try:
            if await check_douyin_http(account_name):
                return HttpLoginResult(account_file=account_file, reused=True)
        except DouyinHttpError:
            pass

    qr_handler = on_qrcode or (lambda url: _show_login_qr(account_name, url))
    with tempfile.TemporaryDirectory(prefix="sau-douyin-login-") as temp_dir:
        staging = Path(temp_dir) / "credentials.json"
        task = {
            "protocol_version": PROTOCOL_VERSION,
            "mode": "login",
            "runtime_src": str(runtime / "src"),
            "timeout": float(timeout),
            "credentials_path": str(staging),
        }
        worker = HttpWorkerProcess(runtime, task)
        try:
            await worker.start()
            result = await worker.wait_result(float(timeout) + _LOGIN_GRACE, on_qrcode=qr_handler)
            if not result.get("success"):
                raise DouyinHttpError(
                    f"抖音 HTTP 扫码登录失败：{result.get('message') or result}"
                )
            try:
                payload = json.loads(staging.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DouyinHttpError("worker 未写出有效凭据文件") from exc
            if not isinstance(payload, dict):
                raise DouyinHttpError("worker 凭据文件格式错误")
            _load_http_credentials(staging)
            payload.setdefault("schema_version", 1)
            payload["platform"] = "douyin"
            payload["account"] = account_name
            payload["engine"] = ENGINE_HTTP
            _write_private_json(account_file, payload)
        finally:
            await worker.aclose()
            _remove_login_qr(account_name)
    return HttpLoginResult(account_file=account_file, reused=False)


def runtime_status() -> dict:
    return {
        "commit": DOUYIN_SPIDER_COMMIT,
        "runtime_dir": str(runtime_dir()),
        "installed": is_runtime_installed(),
        "python": str(venv_python(runtime_dir())),
    }
