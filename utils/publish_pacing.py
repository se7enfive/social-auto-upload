"""发布节奏限制：可选的发布间隔、24 小时提交上限与风险后冷却。

这些是运营层面的确定性上限，只读取本地发布 journal 与本地配置，不访问平台。
本模块不模拟人类操作（不做随机延迟、拟人输入或滚动），也不保证不触发平台风控。
接入点只有 sau_commands 中各上传路径的一行调用，其余逻辑全部在本文件内。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.publish_safety import (
    STATUS_NEEDS_HUMAN,
    PublicationAttempt,
    PublishBlockedError,
    corrupt_record_paths,
    load_records,
    state_root,
)

CONFIG_FILENAME = "publish_pacing.json"
PACING_PLATFORMS = ("douyin", "xiaohongshu")
_RULE_KEYS = ("min_interval_seconds", "max_submits_per_24h", "cooldown_after_risk_seconds")
_MAX_SECONDS = 30 * 24 * 3600
_MAX_SUBMITS = 1000
_WINDOW = timedelta(hours=24)


class PublishPacingError(PublishBlockedError):
    """触发发布节奏限制，本次未点击最终发表。"""


class PublishPacingConfigError(PublishBlockedError):
    """节奏配置无效；为避免静默放宽限制，拒绝自动发表。"""


@dataclass(frozen=True)
class PacingRules:
    """单个平台的节奏规则，0 表示关闭该项。"""

    min_interval_seconds: int = 0
    max_submits_per_24h: int = 0
    cooldown_after_risk_seconds: int = 0

    @property
    def enabled(self) -> bool:
        return any(getattr(self, key) for key in _RULE_KEYS)

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in _RULE_KEYS}


@dataclass(frozen=True)
class PacingDecision:
    allowed: bool
    next_allowed_at: datetime | None = None
    reasons: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return "；".join(self.reasons)


def pacing_config_path() -> Path:
    return state_root() / CONFIG_FILENAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── 配置 ────────────────────────────────────────────────────────────────────


def _config_error(path: Path, detail: str) -> PublishPacingConfigError:
    return PublishPacingConfigError(f"发布节奏配置无效（{path}）: {detail}。修复前拒绝自动发表。")


def _parse_rules(path: Path, platform: str, section) -> PacingRules:
    if not isinstance(section, dict):
        raise _config_error(path, f"{platform} 必须是对象")
    unknown = sorted(set(section) - set(_RULE_KEYS))
    if unknown:
        raise _config_error(path, f"{platform} 包含未知字段 {', '.join(unknown)}")
    values = {}
    for key in _RULE_KEYS:
        value = section.get(key, 0)
        upper = _MAX_SUBMITS if key == "max_submits_per_24h" else _MAX_SECONDS
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper:
            raise _config_error(path, f"{platform}.{key} 必须是 0 到 {upper} 之间的整数，0 表示关闭")
        values[key] = value
    return PacingRules(**values)


def _load_all_rules(path: Path) -> dict[str, PacingRules]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _config_error(path, f"无法读取 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _config_error(path, "顶层必须是对象，键为平台名")
    unknown = sorted(set(payload) - set(PACING_PLATFORMS))
    if unknown:
        raise _config_error(path, f"不支持的平台 {', '.join(unknown)}，可选: {', '.join(PACING_PLATFORMS)}")
    return {platform: _parse_rules(path, platform, section) for platform, section in payload.items()}


def load_pacing_rules(platform: str, path: Path | None = None) -> PacingRules:
    """读取某平台的节奏规则；配置文件不存在时表示不做任何限制。整份文件无效时整体拒绝。"""
    config_path = path or pacing_config_path()
    if not config_path.exists():
        return PacingRules()
    return _load_all_rules(config_path).get(platform, PacingRules())


# ── 判定 ────────────────────────────────────────────────────────────────────


def _is_risk_event(record: dict) -> bool:
    if STATUS_NEEDS_HUMAN in (record.get("status"), record.get("status_before_resolution")):
        return True
    evidence = record.get("evidence")
    return isinstance(evidence, dict) and bool(evidence.get("risk_signal"))


def evaluate_pacing(
    rules: PacingRules,
    records: list[dict],
    *,
    now: datetime,
    unreadable_count: int = 0,
) -> PacingDecision:
    """纯函数判定：只依据记录与当前时间，不读写文件。

    - 只统计进入过最终点击边界的记录（submit_attempted），准备与点击前失败不计入。
    - 无法读取或缺少时间的记录会使判定失败，避免静默放宽限制。
    """
    if not rules.enabled:
        return PacingDecision(allowed=True)

    unreadable = unreadable_count
    attempt_times: list[datetime] = []
    risk_times: list[datetime] = []
    for record in records:
        if record.get("submit_attempted"):
            when = _parse_time(record.get("submit_attempted_at")) or _parse_time(record.get("updated_at"))
            if when is None:
                unreadable += 1
            else:
                attempt_times.append(when)
        if _is_risk_event(record):
            when = _parse_time(record.get("updated_at"))
            if when is None:
                unreadable += 1
            else:
                risk_times.append(when)
    if unreadable:
        return PacingDecision(
            allowed=False,
            reasons=(f"有 {unreadable} 条发布记录无法读取或缺少时间，无法判断节奏，请先人工检查发布记录目录",),
        )

    attempt_times.sort()
    blockers: list[tuple[datetime, str]] = []
    if rules.min_interval_seconds and attempt_times:
        next_at = attempt_times[-1] + timedelta(seconds=rules.min_interval_seconds)
        if now < next_at:
            blockers.append((next_at, f"距离上一次正式提交尝试不足 {rules.min_interval_seconds} 秒"))
    if rules.max_submits_per_24h:
        recent = [when for when in attempt_times if when > now - _WINDOW]
        if len(recent) >= rules.max_submits_per_24h:
            # 只有最早的那一次滑出 24 小时窗口，才会空出一个名额。
            next_at = recent[-rules.max_submits_per_24h] + _WINDOW
            blockers.append(
                (next_at, f"近 24 小时内已有 {len(recent)} 次正式提交尝试，达到上限 {rules.max_submits_per_24h}")
            )
    if rules.cooldown_after_risk_seconds and risk_times:
        next_at = max(risk_times) + timedelta(seconds=rules.cooldown_after_risk_seconds)
        if now < next_at:
            blockers.append((next_at, f"最近一次验证或风险记录距今不足 {rules.cooldown_after_risk_seconds} 秒"))

    if not blockers:
        return PacingDecision(allowed=True)
    # 各限制随时间单调解除，全部解除的时刻即最晚的那个 next_at。
    next_allowed = max(next_at for next_at, _ in blockers)
    reasons = tuple(f"{cause}，最早可在 {_fmt(next_at)} 再次正式发布" for next_at, cause in blockers)
    return PacingDecision(allowed=False, next_allowed_at=next_allowed, reasons=reasons)


# ── 接入点 ──────────────────────────────────────────────────────────────────


def _journal_state(platform: str, account_name: str) -> tuple[list[dict], int]:
    return load_records(platform, account_name), len(corrupt_record_paths(platform, account_name))


def enforce_publish_pacing(attempt: PublicationAttempt, *, now: datetime | None = None) -> None:
    """正式发表前的节奏检查。调用方需已持有账号锁，且已完成幂等检查。"""
    if attempt.prepare_only:
        return
    rules = load_pacing_rules(attempt.platform)
    if not rules.enabled:
        return
    records, unreadable = _journal_state(attempt.platform, attempt.account_name)
    decision = evaluate_pacing(rules, records, now=now or _utc_now(), unreadable_count=unreadable)
    if not decision.allowed:
        raise PublishPacingError(
            f"账号 {attempt.account_name} 触发发布节奏限制：{decision.message}。"
            "未点击最终发表；--prepare-only 准备不受此限制。"
        )


def pacing_snapshot(platform: str, account_name: str, *, now: datetime | None = None) -> dict:
    """供 `sau safety status` 使用：只读本地配置与 journal，不访问平台。"""
    path = pacing_config_path()
    try:
        rules = load_pacing_rules(platform, path)
    except PublishPacingConfigError as exc:
        return {"config_path": str(path), "error": str(exc)}
    if not rules.enabled:
        return {
            "config_path": str(path),
            "enabled": False,
            "allowed_now": True,
            "next_allowed_at": None,
            "reasons": [],
        }
    records, unreadable = _journal_state(platform, account_name)
    decision = evaluate_pacing(rules, records, now=now or _utc_now(), unreadable_count=unreadable)
    next_allowed = decision.next_allowed_at
    return {
        "config_path": str(path),
        "enabled": True,
        "rules": rules.as_dict(),
        "allowed_now": decision.allowed,
        "next_allowed_at": next_allowed.astimezone().isoformat(timespec="seconds") if next_allowed else None,
        "reasons": list(decision.reasons),
    }
