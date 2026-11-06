"""本地发布安全记录命令；不读取 cookie、不访问平台、不触发重发。"""
from __future__ import annotations

import json

from sau_commands.arguments import resolve_account_file
from utils.publish_safety import (
    corrupt_record_paths,
    find_unresolved,
    load_records,
    resolve_record,
    session_lock,
)

SUPPORTED_PLATFORMS = ("douyin", "xiaohongshu")
_PUBLIC_FIELDS = (
    "run_id",
    "platform",
    "account",
    "request_id",
    "status",
    "submit_attempted",
    "prepare_only",
    "created_at",
    "updated_at",
    "summary",
    "reason",
    "resolution",
    "status_before_resolution",
)


def register(platform_parsers):
    """声明本地 safety 命令。"""
    safety_parser = platform_parsers.add_parser(
        "safety", help="Inspect or resolve local publication safety records"
    )
    safety_actions = safety_parser.add_subparsers(dest="action", required=True)

    status_parser = safety_actions.add_parser(
        "status", help="Show unresolved publication records for one account"
    )
    status_parser.add_argument(
        "--platform", dest="target_platform", required=True, choices=SUPPORTED_PLATFORMS
    )
    status_parser.add_argument("--account", required=True, help="User-defined account_name")

    resolve_parser = safety_actions.add_parser(
        "resolve", help="Record a human verification outcome for an unresolved run"
    )
    resolve_parser.add_argument(
        "--platform", dest="target_platform", required=True, choices=SUPPORTED_PLATFORMS
    )
    resolve_parser.add_argument("--account", required=True, help="User-defined account_name")
    resolve_parser.add_argument("--run-id", required=True, help="Run id shown by `sau safety status`")
    resolve_parser.add_argument(
        "--outcome",
        required=True,
        choices=("submitted", "not-submitted"),
        help="Human-verified outcome; not-submitted allows a later retry",
    )
    resolve_parser.add_argument(
        "--reason", required=True, help="How the human verified the outcome (audit trail)"
    )


def _public_record(record: dict) -> dict:
    return {field: record.get(field) for field in _PUBLIC_FIELDS if field in record}


async def run(args):
    if args.action == "status":
        records = load_records(args.target_platform, args.account)
        unresolved = [record for record in records if record.get("status") in {"submission_unknown", "needs_human"}]
        payload = {
            "platform": args.target_platform,
            "account": args.account,
            "unresolved": [_public_record(record) for record in unresolved],
            "recent": [_public_record(record) for record in records[:5]],
            "corrupt_files": corrupt_record_paths(args.target_platform, args.account),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.action == "resolve":
        account_file = resolve_account_file(args.target_platform, args.account)
        with session_lock(account_file, args.target_platform, args.account):
            record = resolve_record(
                args.target_platform,
                args.account,
                args.run_id,
                args.outcome,
                args.reason,
            )
        print(json.dumps(_public_record(record), ensure_ascii=False, indent=2))
        return 0

    raise RuntimeError(f"Unsupported safety action: {args.action}")
