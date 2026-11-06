"""发布安全保护层：本地会话锁、原子 journal、期限与单次提交控制。

设计边界：
- 只保证同一本地会话文件不并发；不同别名可能绑定同一远端账号，本模块不做远端身份去重。
- journal 只记录发布状态与必要证据，不保存 cookie、验证码、storage_state 或认证 URL。
- 最终发表点击必须先持久化提交意图；点击之后结果不明时保持 submission_unknown，绝不盲目重试。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from conf import BASE_DIR

logger = logging.getLogger(__name__)

DEFAULT_PUBLISH_TIMEOUT = 1800.0
MAX_SUBMIT_OBSERVATION = 120.0
CLEANUP_GRACE = 15.0

STATUS_PREPARING = "preparing"
STATUS_PREPARED = "prepared"
STATUS_SUBMISSION_UNKNOWN = "submission_unknown"
STATUS_SUBMITTED = "submitted"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_FAILED_BEFORE_SUBMIT = "failed_before_submit"
STATUS_NOT_SUBMITTED = "not_submitted"

BLOCKING_STATUSES = frozenset({STATUS_SUBMISSION_UNKNOWN, STATUS_NEEDS_HUMAN})
TERMINAL_STATUSES = frozenset(
    {
        STATUS_PREPARED,
        STATUS_SUBMITTED,
        STATUS_SUBMISSION_UNKNOWN,
        STATUS_NEEDS_HUMAN,
        STATUS_NOT_SUBMITTED,
    }
)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class PublishSafetyError(RuntimeError):
    """发布保护层的基础异常。"""


class PublishLockError(PublishSafetyError):
    """同一本地会话已有其他进程在操作。"""


class PublishBlockedError(PublishSafetyError):
    """账号存在未解决的提交记录，拒绝继续自动发表。"""


class PublishAlreadySubmitted(PublishSafetyError):
    """同一 request-id 已提交过，按幂等语义跳过。"""


class PublishRequestConflict(PublishSafetyError):
    """同一 request-id 对应了不同内容。"""


class PublishJournalError(PublishSafetyError):
    """发布 journal 无法可靠写入。"""


class PublishDeadlineExceeded(TimeoutError):
    """发布总预算已耗尽。"""


class PublishOutcomeUnknown(PublishSafetyError):
    """最终提交已尝试但结果无法确认，可能已经发布。"""


class PublishNeedsHuman(PublishSafetyError):
    """需要人工完成验证或核对，未点击最终发表。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_component(value: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")[:max_len]
    if not slug:
        slug = "account"
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _state_root() -> Path:
    """本地保护状态根目录；SAU_STATE_DIR 仅供测试或自定义部署覆盖。"""
    override = os.environ.get("SAU_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(BASE_DIR) / ".local"


def state_root() -> Path:
    """供同层模块（如发布节奏配置）读取本地状态根目录。"""
    return _state_root()


def account_journal_dir(platform: str, account_name: str) -> Path:
    return _state_root() / "publications" / platform / _safe_component(account_name)


def scoped_verify_code_path(platform: str, account_file: str | Path) -> Path:
    normalized = os.path.normcase(str(Path(account_file).expanduser().resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return _state_root() / "verify_codes" / f"{platform}-{digest}.txt"


def validate_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if not _REQUEST_ID_RE.fullmatch(cleaned):
        raise ValueError(
            "request-id 只能包含字母、数字、点、下划线、冒号和连字符，长度 1-128；"
            "不接受路径分隔符"
        )
    return cleaned


def validate_timeout(value: float | int | str | None) -> float:
    if value is None:
        return DEFAULT_PUBLISH_TIMEOUT
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeout 必须是有限正数: {value!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout 必须是有限正数: {value!r}")
    return timeout


@dataclass(slots=True)
class Deadline:
    timeout_seconds: float
    _expires_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.timeout_seconds = validate_timeout(self.timeout_seconds)
        self._expires_at = time.monotonic() + self.timeout_seconds

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())

    def clamp(self, seconds: float, minimum: float = 0.5) -> float:
        return max(minimum, min(float(seconds), self.remaining()))

    def raise_if_expired(self, context: str = "") -> None:
        if self.expired:
            message = "发布总预算已耗尽"
            if context:
                message = f"{message}: {context}"
            raise PublishDeadlineExceeded(message)


# ── 原子 JSON 写入 ──────────────────────────────────────────────────────────


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: str | Path, payload: dict) -> None:
    """同目录临时文件 + fsync + os.replace，避免读到半截 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise PublishJournalError(f"无法写入发布记录 {target}: {exc}") from exc


def _read_record(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_records(platform: str, account_name: str) -> list[dict]:
    """读取某账号的发布记录；损坏文件会被跳过并在 status 中单独提示。"""
    runs_dir = account_journal_dir(platform, account_name) / "runs"
    if not runs_dir.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(runs_dir.glob("*.json")):
        record = _read_record(path)
        if record is None:
            continue
        record.setdefault("run_id", path.stem)
        record["_path"] = str(path)
        records.append(record)
    records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return records


def corrupt_record_paths(platform: str, account_name: str) -> list[str]:
    runs_dir = account_journal_dir(platform, account_name) / "runs"
    if not runs_dir.is_dir():
        return []
    corrupt: list[str] = []
    for path in sorted(runs_dir.glob("*.json")):
        if _read_record(path) is None:
            corrupt.append(str(path))
    return corrupt


def find_unresolved(platform: str, account_name: str) -> list[dict]:
    return [
        record
        for record in load_records(platform, account_name)
        if record.get("status") in BLOCKING_STATUSES
    ]


def resolve_record(
    platform: str,
    account_name: str,
    run_id: str,
    outcome: str,
    reason: str,
) -> dict:
    """保存人工核对结论；不访问平台、不触发重发。"""
    if outcome not in {"submitted", "not-submitted"}:
        raise ValueError("outcome 只能是 submitted 或 not-submitted")
    if not _RUN_ID_RE.fullmatch(str(run_id)):
        raise ValueError(f"非法 run-id: {run_id!r}")
    if not str(reason or "").strip():
        raise ValueError("resolve 必须提供人工核对依据 --reason")

    path = account_journal_dir(platform, account_name) / "runs" / f"{run_id}.json"
    record = _read_record(path)
    if record is None:
        raise RuntimeError(f"未找到发布记录: {run_id}")
    if record.get("status") not in BLOCKING_STATUSES:
        raise RuntimeError(
            f"记录 {run_id} 当前状态为 {record.get('status')}，只有 {sorted(BLOCKING_STATUSES)} 需要人工处理"
        )

    record["status_before_resolution"] = record.get("status")
    record["status"] = STATUS_SUBMITTED if outcome == "submitted" else STATUS_NOT_SUBMITTED
    record["resolution"] = {
        "outcome": outcome,
        "reason": str(reason).strip(),
        "resolved_at": _now(),
    }
    record["updated_at"] = record["resolution"]["resolved_at"]
    atomic_write_json(path, record)
    return record


# ── 本地会话 OS 锁 ──────────────────────────────────────────────────────────

_held_locks: set[str] = set()


def _lock_path(account_file: str | Path, platform: str) -> Path:
    normalized = os.path.normcase(str(Path(account_file).expanduser().resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return _state_root() / "locks" / f"{platform}-{digest}.lock"


def _lock_file(file_obj) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file_obj) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _lock_holder(lock_path: Path) -> str:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "未知进程"
    pid = payload.get("pid")
    started = payload.get("acquired_at")
    return f"pid={pid}, acquired_at={started}"


@contextmanager
def session_lock(account_file: str | Path, platform: str, account_name: str):
    """非阻塞获取账号级跨进程锁；冲突立即失败，不轮询抢占。"""
    lock_path = _lock_path(account_file, platform)
    lock_key = str(lock_path)
    if lock_key in _held_locks:
        raise PublishLockError(
            f"当前进程已持有账号 {account_name} 的发布锁，拒绝重入: {lock_path}"
        )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    file_obj = open(lock_path, "a+b")
    try:
        file_obj.seek(0, os.SEEK_END)
        if file_obj.tell() == 0:
            file_obj.write(b"\0")
            file_obj.flush()
        file_obj.seek(0)
        try:
            _lock_file(file_obj)
        except OSError as exc:
            raise PublishLockError(
                f"账号 {account_name} 正在被另一个进程操作（{_lock_holder(lock_path)}），"
                f"本次不并发执行。锁文件: {lock_path}"
            ) from exc

        _held_locks.add(lock_key)
        try:
            file_obj.seek(0)
            file_obj.truncate()
            file_obj.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "platform": platform,
                        "account": account_name,
                        "acquired_at": _now(),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            file_obj.flush()
            yield lock_path
        finally:
            _held_locks.discard(lock_key)
            try:
                _unlock_file(file_obj)
            except OSError:
                pass
    finally:
        file_obj.close()


# ── 内容指纹 ────────────────────────────────────────────────────────────────


def content_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_signature(path: str | Path) -> dict:
    file_path = Path(path).expanduser()
    try:
        stat = file_path.stat()
    except OSError:
        return {"path": str(file_path)}
    return {
        "path": str(file_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


# ── 发布尝试 ────────────────────────────────────────────────────────────────


class PublicationAttempt:
    """一次发布尝试的本地状态机。

    状态迁移：
    preparing -> prepared（--prepare-only）
    preparing -> submitting -> submitted / submission_unknown
    preparing -> needs_human（点击前明确验证/风险）
    preparing -> failed_before_submit（点击前失败，可安全重试）
    """

    def __init__(
        self,
        *,
        platform: str,
        account_name: str,
        account_file: str | Path,
        fingerprint: str,
        request_id: str | None = None,
        prepare_only: bool = False,
        timeout: float | int = DEFAULT_PUBLISH_TIMEOUT,
        summary: dict | None = None,
    ) -> None:
        self.platform = platform
        self.account_name = account_name
        self.account_file = Path(account_file)
        self.fingerprint = fingerprint
        self.request_id = validate_request_id(request_id)
        self.prepare_only = bool(prepare_only)
        self.deadline = Deadline(timeout)
        self.run_id = uuid.uuid4().hex
        self.created_at = _now()
        self.updated_at = self.created_at
        self.status = STATUS_PREPARING
        self.submit_attempted = False
        self.submit_attempted_at: str | None = None
        self.reason = ""
        self.summary = summary or {}
        self.evidence: dict = {}
        self._run_path = account_journal_dir(platform, account_name) / "runs" / f"{self.run_id}.json"

    # ── 状态持久化 ──
    def _record(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "platform": self.platform,
            "account": self.account_name,
            "request_id": self.request_id,
            "content_fingerprint": self.fingerprint,
            "status": self.status,
            "submit_attempted": self.submit_attempted,
            "submit_attempted_at": self.submit_attempted_at,
            "prepare_only": self.prepare_only,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "evidence": self.evidence,
            "reason": self.reason,
        }

    def _write(self) -> None:
        self.updated_at = _now()
        atomic_write_json(self._run_path, self._record())

    def _write_best_effort(self) -> None:
        try:
            self._write()
        except PublishJournalError as exc:
            logger.warning("发布状态无法持久化，保留内存状态 %s: %s", self.status, exc)

    # ── 启动前检查 ──
    def ensure_can_start(self) -> None:
        records = load_records(self.platform, self.account_name)
        blocking = [record for record in records if record.get("status") in BLOCKING_STATUSES]
        if blocking:
            details = ", ".join(
                f"{record.get('run_id')}({record.get('status')})" for record in blocking[:5]
            )
            raise PublishBlockedError(
                f"账号 {self.account_name} 存在未解决的发布记录: {details}。"
                "请先到平台核对原作品，再运行 `sau safety resolve ...` 记录人工结论。"
            )

        if not self.request_id:
            return

        for record in records:
            if record.get("request_id") != self.request_id:
                continue
            if record.get("content_fingerprint") != self.fingerprint:
                raise PublishRequestConflict(
                    f"request-id {self.request_id} 已用于不同内容"
                    f"（run {record.get('run_id')}），拒绝发布。"
                )
            status = record.get("status")
            if status in {STATUS_SUBMITTED, STATUS_SUBMISSION_UNKNOWN}:
                raise PublishAlreadySubmitted(
                    f"request-id {self.request_id} 已提交过"
                    f"（status={status}, run {record.get('run_id')}），不会重复发表。"
                )
            if status == STATUS_NEEDS_HUMAN:
                raise PublishBlockedError(
                    f"request-id {self.request_id} 仍在等待人工处理"
                    f"（run {record.get('run_id')}）。"
                )

    # ── 状态迁移 ──
    def begin(self) -> None:
        self._write()

    def mark_prepared(self, evidence: dict | None = None) -> None:
        if evidence:
            self.evidence.update(evidence)
        previous_status = self.status
        self.status = STATUS_PREPARED
        try:
            self._write()
        except Exception:
            # 写入失败时不能对外声称已准备完成。
            self.status = previous_status
            raise

    def prepare_submit(self, evidence: dict | None = None) -> None:
        """点击前持久化提交意图；写入失败必须阻止点击。

        意图一旦落盘就按 submission_unknown 处理：进程若在点击前后崩溃，
        下一次运行必须人工核对，不能自动重发。写入失败表示意图未落盘，
        恢复原状态，调用方可以安全地按“点击前失败”处理。
        """
        if evidence:
            self.evidence.update(evidence)
        previous_status = self.status
        previous_attempted = self.submit_attempted
        previous_attempted_at = self.submit_attempted_at
        self.status = STATUS_SUBMISSION_UNKNOWN
        self.submit_attempted = True
        self.submit_attempted_at = _now()
        try:
            self._write()
        except Exception:
            self.status = previous_status
            self.submit_attempted = previous_attempted
            self.submit_attempted_at = previous_attempted_at
            raise

    def mark_submitted(self, evidence: dict | None = None) -> None:
        if evidence:
            self.evidence.update(evidence)
        self.status = STATUS_SUBMITTED
        self.reason = ""
        self._write_best_effort()

    def mark_unknown(self, reason: str, evidence: dict | None = None) -> None:
        if evidence:
            self.evidence.update(evidence)
        self.status = STATUS_SUBMISSION_UNKNOWN
        self.submit_attempted = True
        self.reason = str(reason)
        self._write_best_effort()

    def mark_needs_human(self, reason: str, evidence: dict | None = None) -> None:
        if evidence:
            self.evidence.update(evidence)
        self.status = STATUS_NEEDS_HUMAN
        self.reason = str(reason)
        self._write_best_effort()

    def mark_failed_before_submit(self, reason: str) -> None:
        if self.submit_attempted:
            return
        self.status = STATUS_FAILED_BEFORE_SUBMIT
        self.reason = str(reason)
        self._write_best_effort()

    def finalize_on_error(self, reason: str) -> None:
        if self.status in TERMINAL_STATUSES:
            return
        if self.submit_attempted:
            self.mark_unknown(reason)
        else:
            self.mark_failed_before_submit(reason)

    def export(self, path: str | Path | None) -> None:
        if not path:
            return
        try:
            atomic_write_json(Path(path), self._record())
        except Exception as exc:  # 导出失败不能影响真实发布结论
            logger.warning("发布结果导出失败 %s: %s", path, exc)

    async def run_with_deadline(self, coroutine):
        remaining = self.deadline.remaining()
        if remaining <= 0:
            self.finalize_on_error("发布总预算在启动前已耗尽")
            raise PublishDeadlineExceeded("发布总预算在启动前已耗尽")
        try:
            return await asyncio.wait_for(coroutine, timeout=remaining + CLEANUP_GRACE)
        except asyncio.TimeoutError as exc:
            self.finalize_on_error("发布总预算已耗尽")
            raise PublishDeadlineExceeded("发布总预算已耗尽，任务已停止") from exc


# ── 单次提交 ────────────────────────────────────────────────────────────────


async def submit_once(
    *,
    safety: PublicationAttempt | None,
    click,
    wait_success,
    detect_risk=None,
    observation_timeout: float = MAX_SUBMIT_OBSERVATION,
    evidence: dict | None = None,
) -> None:
    """最终发表只点击一次；点击后只观察，不再重复点击。

    click: async callable，执行一次最终点击。
    wait_success: async callable(timeout_seconds)，成功时返回，失败时抛异常。
    detect_risk: async callable -> str | None，返回风险原因。
    """
    if safety is not None:
        safety.prepare_submit(evidence)  # 写入失败会抛异常，从而不会点击

    try:
        await click()
    except asyncio.CancelledError:
        if safety is not None:
            safety.mark_unknown("提交点击期间任务被取消，结果未知")
        raise
    except Exception as exc:
        if safety is not None:
            safety.mark_unknown(f"提交点击抛出异常: {exc}")
        raise PublishOutcomeUnknown(
            f"最终发表点击失败，结果未知，可能已经提交: {exc}"
        ) from exc

    timeout = max(0.5, float(observation_timeout))
    if safety is not None:
        timeout = safety.deadline.clamp(timeout)

    try:
        await wait_success(timeout)
    except asyncio.CancelledError:
        if safety is not None:
            safety.mark_unknown("等待提交结果期间任务被取消，结果未知")
        raise
    except Exception as exc:
        risk_reason = None
        if detect_risk is not None:
            try:
                risk_reason = await detect_risk()
            except Exception:
                risk_reason = None
        reason = f"提交结果未确认: {exc}"
        risk_evidence = None
        if risk_reason:
            reason = f"{reason}; 风险信号: {risk_reason}"
            risk_evidence = {"risk_signal": risk_reason}
        if safety is not None:
            safety.mark_unknown(reason, risk_evidence)
        raise PublishOutcomeUnknown(
            f"最终发表已尝试但结果未确认，可能已经提交。{reason}。"
            "请先到平台核对作品，不要直接重发。"
        ) from exc

    if safety is not None:
        safety.mark_submitted(evidence)


async def close_browser_resources(context, browser, *, timeout: float = 5.0) -> list[str]:
    """分别有界关闭 context 与 browser；context 失败不得跳过 browser。"""
    errors: list[str] = []
    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=timeout)
        except Exception as exc:
            errors.append(f"context.close: {exc}")
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=timeout)
        except Exception as exc:
            errors.append(f"browser.close: {exc}")
    for error in errors:
        logger.warning("浏览器资源清理失败: %s", error)
    return errors
