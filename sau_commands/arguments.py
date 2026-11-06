"""CLI 共用的路径、标签和时间参数规则。"""
from __future__ import annotations
import argparse
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable
from conf import BASE_DIR
from utils.publish_safety import DEFAULT_PUBLISH_TIMEOUT

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"

def has_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_account_file(platform: str, account_name: str) -> Path:
    account_file = Path(BASE_DIR) / "cookies" / f"{platform}_{account_name}.json"
    account_file.parent.mkdir(exist_ok=True)
    return account_file


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


def add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headed", dest="headless", action="store_false", help="Run with browser UI")
    headless_group.add_argument("--headless", dest="headless", action="store_true", help="Run in headless mode")
    parser.set_defaults(headless=True)
