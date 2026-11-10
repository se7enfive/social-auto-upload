"""CLI 共用的路径、标签和时间参数规则。"""
from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable
from conf import BASE_DIR
from utils.publish_safety import DEFAULT_PUBLISH_TIMEOUT

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"
UPLOAD_ENGINES = ("auto", "browser", "http")


def default_upload_engine() -> str:
    """CLI 默认引擎；conf.DOUYIN_UPLOAD_ENGINE 可覆盖，缺省 auto。"""
    try:
        import conf

        value = str(getattr(conf, "DOUYIN_UPLOAD_ENGINE", "auto") or "auto").strip().lower()
    except Exception:
        value = "auto"
    return value if value in UPLOAD_ENGINES else "auto"

def has_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cookies_dir() -> Path:
    """浏览器 storage_state 与 HTTP 凭据的根目录；SAU_COOKIES_DIR 可覆盖。"""
    override = os.environ.get("SAU_COOKIES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(BASE_DIR) / "cookies"


def resolve_account_file(platform: str, account_name: str) -> Path:
    account_file = cookies_dir() / f"{platform}_{account_name}.json"
    account_file.parent.mkdir(parents=True, exist_ok=True)
    return account_file


def resolve_http_account_file(platform: str, account_name: str) -> Path:
    """HTTP 引擎的扫码票据凭据；与浏览器 storage_state 分开保存。"""
    return cookies_dir() / f"{platform}_http_{account_name}.json"


def parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []

    tags: list[str] = []
    for item in raw_tags.split(","):
        cleaned = item.strip().lstrip("#")
        if cleaned:
            tags.append(cleaned)
    return tags


def parse_image_files(raw_files: Iterable[Path]) -> list[Path]:
    return [Path(file) for file in raw_files]


def parse_schedule(raw_schedule: str | None) -> datetime | int:
    if not raw_schedule:
        return 0
    return datetime.strptime(raw_schedule, SCHEDULE_FORMAT)


def existing_file_path(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"File not found: {value}")
    return path


def schedule_value(value: str):
    try:
        return parse_schedule(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid schedule '{value}'. Expected format: {SCHEDULE_FORMAT}"
        ) from exc


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"Expected a positive number: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected a finite positive number: {value}")
    return parsed


def add_publish_safety_flags(parser: argparse.ArgumentParser) -> None:
    """发布保护参数；默认值保持旧命令的真实发表语义。"""
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Upload media and fill the form, but do not click the final publish button",
    )
    parser.add_argument(
        "--result",
        type=Path,
        help="Optional path to export a structured publication result",
    )
    parser.add_argument(
        "--request-id",
        default=None,
        help="Stable idempotency key; the same id with different content is rejected",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_PUBLISH_TIMEOUT,
        help=f"Total execution budget in seconds (default: {DEFAULT_PUBLISH_TIMEOUT:g})",
    )


def publish_safety_options(args: argparse.Namespace) -> dict:
    """兼容手工构造的 Namespace：缺少新字段时使用安全默认值。"""
    return {
        "prepare_only": bool(getattr(args, "prepare_only", False)),
        "result_file": getattr(args, "result", None),
        "request_id": getattr(args, "request_id", None),
        "timeout": getattr(args, "timeout", DEFAULT_PUBLISH_TIMEOUT),
    }


class _HeadlessUnsupportedAction(argparse.Action):
    """在参数解析阶段拒绝 --headless，避免启动无头 Chromium。

    抖音、小红书、视频号只允许本机 Chrome 有头模式；直接 parser.error()
    会在启动任何浏览器之前以退出码 2 结束。
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            f"{option_string} is not supported for this command: "
            "it always launches a visible local Google Chrome window."
        )


def add_runtime_flags(
    parser: argparse.ArgumentParser,
    *,
    default_headless: bool = True,
    allow_headless: bool = True,
) -> None:
    """添加 --debug/--headed/--headless。

    allow_headless=False 时仍保留 --headless（便于给出明确错误），但解析即失败；
    默认值由 default_headless 决定。
    """
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headed", dest="headless", action="store_false", help="Run with browser UI")
    if allow_headless:
        headless_group.add_argument("--headless", dest="headless", action="store_true", help="Run in headless mode")
    else:
        headless_group.add_argument(
            "--headless",
            dest="headless",
            action=_HeadlessUnsupportedAction,
            help="Not supported: this command always launches a visible local Chrome window",
        )
    parser.set_defaults(headless=default_headless)
