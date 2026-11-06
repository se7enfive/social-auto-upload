"""xiaohongshu 的命令参数与执行逻辑。"""
from __future__ import annotations
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuNote,
    XiaoHongShuVideo,
    cookie_auth as xiaohongshu_cookie_auth,
    xiaohongshu_setup,
)
from sau_commands.arguments import (
    SCHEDULE_FORMAT,
    add_publish_safety_flags,
    add_runtime_flags,
    existing_file_path,
    parse_image_files,
    parse_tags,
    publish_safety_options,
    resolve_account_file,
    schedule_value,
)
from utils.publish_safety import (
    DEFAULT_PUBLISH_TIMEOUT,
    PublicationAttempt,
    PublishAlreadySubmitted,
    content_fingerprint,
    file_signature,
    session_lock,
)


def register(platform_parsers):
    """声明本平台命令、参数及合法选项。"""
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    xiaohongshu_parser = platform_parsers.add_parser("xiaohongshu", help="Xiaohongshu operations")
    xiaohongshu_actions = xiaohongshu_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = xiaohongshu_actions.add_parser(action_name, help=f"Xiaohongshu {action_name}")
        action_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    xiaohongshu_upload_video_parser = xiaohongshu_actions.add_parser("upload-video", help="Upload one video to Xiaohongshu")
    xiaohongshu_upload_video_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
    xiaohongshu_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    xiaohongshu_upload_video_parser.add_argument("--title", required=True, help="Video title")
    xiaohongshu_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    xiaohongshu_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    xiaohongshu_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    xiaohongshu_upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional thumbnail path")
    add_publish_safety_flags(xiaohongshu_upload_video_parser)
    add_runtime_flags(xiaohongshu_upload_video_parser)

    xiaohongshu_upload_note_parser = xiaohongshu_actions.add_parser("upload-note", help="Upload one note to Xiaohongshu")
    xiaohongshu_upload_note_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
    xiaohongshu_upload_note_parser.add_argument("--images", required=True, nargs="+", type=existing_file_path, help="Image file paths")
    xiaohongshu_upload_note_parser.add_argument("--title", required=True, help="Note title")
    xiaohongshu_upload_note_parser.add_argument("--note", default="", help="Optional note content")
    xiaohongshu_upload_note_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    xiaohongshu_upload_note_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    add_publish_safety_flags(xiaohongshu_upload_note_parser)
    add_runtime_flags(xiaohongshu_upload_note_parser)


@dataclass(slots=True)
class XiaohongshuVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    prepare_only: bool = False
    result_file: Path | None = None
    request_id: str | None = None
    timeout: float = DEFAULT_PUBLISH_TIMEOUT


@dataclass(slots=True)
class XiaohongshuNoteUploadRequest:
    account_name: str
    image_files: list[Path]
    title: str
    note: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    prepare_only: bool = False
    result_file: Path | None = None
    request_id: str | None = None
    timeout: float = DEFAULT_PUBLISH_TIMEOUT


async def login_xiaohongshu_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("xiaohongshu", account_name)
    with session_lock(account_file, "xiaohongshu", account_name):
        return await xiaohongshu_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_xiaohongshu_account(account_name: str) -> bool:
    account_file = resolve_account_file("xiaohongshu", account_name)
    with session_lock(account_file, "xiaohongshu", account_name):
        if not account_file.exists():
            return False
        return await xiaohongshu_cookie_auth(str(account_file))


def _schedule_fingerprint(publish_date: datetime | int) -> str | int:
    return publish_date.isoformat() if isinstance(publish_date, datetime) else 0


def _video_fingerprint(request: XiaohongshuVideoUploadRequest) -> str:
    return content_fingerprint(
        {
            "platform": "xiaohongshu",
            "kind": "video",
            "title": request.title,
            "description": request.description,
            "tags": request.tags,
            "publish_date": _schedule_fingerprint(request.publish_date),
            "media": [file_signature(request.video_file)],
            "thumbnail": file_signature(request.thumbnail_file) if request.thumbnail_file else None,
        }
    )


def _note_fingerprint(request: XiaohongshuNoteUploadRequest) -> str:
    return content_fingerprint(
        {
            "platform": "xiaohongshu",
            "kind": "note",
            "title": request.title,
            "note": request.note,
            "tags": request.tags,
            "publish_date": _schedule_fingerprint(request.publish_date),
            "media": [file_signature(path) for path in request.image_files],
        }
    )


async def upload_xiaohongshu_video(request: XiaohongshuVideoUploadRequest) -> Path:
    account_file = resolve_account_file("xiaohongshu", request.account_name)
    fingerprint = _video_fingerprint(request)
    with session_lock(account_file, "xiaohongshu", request.account_name):
        attempt = PublicationAttempt(
            platform="xiaohongshu",
            account_name=request.account_name,
            account_file=account_file,
            fingerprint=fingerprint,
            request_id=request.request_id,
            prepare_only=request.prepare_only,
            timeout=request.timeout,
            summary={
                "kind": "video",
                "title": request.title,
                "media_count": 1,
                "scheduled": isinstance(request.publish_date, datetime),
            },
        )
        attempt.ensure_can_start()
        attempt.begin()
        try:
            is_ready = await xiaohongshu_setup(str(account_file), handle=False)
            if not is_ready:
                raise RuntimeError(
                    f"Xiaohongshu cookie is missing or expired: {account_file}. Run `sau xiaohongshu login --account {request.account_name}` first."
                )

            app = XiaoHongShuVideo(
                title=request.title,
                file_path=str(request.video_file),
                desc=request.description,
                tags=request.tags,
                publish_date=request.publish_date,
                account_file=str(account_file),
                thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
                publish_strategy=request.publish_strategy,
                debug=request.debug,
                headless=request.headless,
                safety=attempt,
            )
            await attempt.run_with_deadline(app.main())
        except Exception as exc:
            attempt.finalize_on_error(str(exc))
            raise
        finally:
            # 失败时也导出最终记录，便于调用方读取真实状态；导出失败不影响原始结果。
            attempt.export(request.result_file)
    return account_file


async def upload_xiaohongshu_note(request: XiaohongshuNoteUploadRequest) -> Path:
    account_file = resolve_account_file("xiaohongshu", request.account_name)
    fingerprint = _note_fingerprint(request)
    with session_lock(account_file, "xiaohongshu", request.account_name):
        attempt = PublicationAttempt(
            platform="xiaohongshu",
            account_name=request.account_name,
            account_file=account_file,
            fingerprint=fingerprint,
            request_id=request.request_id,
            prepare_only=request.prepare_only,
            timeout=request.timeout,
            summary={
                "kind": "note",
                "title": request.title,
                "media_count": len(request.image_files),
                "scheduled": isinstance(request.publish_date, datetime),
            },
        )
        attempt.ensure_can_start()
        attempt.begin()
        try:
            is_ready = await xiaohongshu_setup(str(account_file), handle=False)
            if not is_ready:
                raise RuntimeError(
                    f"Xiaohongshu cookie is missing or expired: {account_file}. Run `sau xiaohongshu login --account {request.account_name}` first."
                )

            app = XiaoHongShuNote(
                image_paths=[str(path) for path in request.image_files],
                title=request.title,
                desc=request.note,
                note=request.note,
                tags=request.tags,
                publish_date=request.publish_date,
                account_file=str(account_file),
                publish_strategy=request.publish_strategy,
                debug=request.debug,
                headless=request.headless,
                safety=attempt,
            )
            await attempt.run_with_deadline(app.main())
        except Exception as exc:
            attempt.finalize_on_error(str(exc))
            raise
        finally:
            attempt.export(request.result_file)
    return account_file


async def run(args):
    """执行本平台命令；发布结果由平台实现核验。"""
    if args.action == "login":
        result = await login_xiaohongshu_account(args.account, headless=args.headless)
        if not result["success"]:
            raise RuntimeError(result["message"])
        print(f"Xiaohongshu login flow completed: {result['account_file']}")
        return 0

    if args.action == "check":
        is_valid = await check_xiaohongshu_account(args.account)
        print("valid" if is_valid else "invalid")
        return 0 if is_valid else 1

    publish_strategy = (
        XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED if args.schedule else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    )

    if args.action == "upload-video":
        safety_options = publish_safety_options(args)
        parsed_tags = parse_tags(args.tags)
        if len(parsed_tags) > 10:
            print(f"错误：小红书标签最多 10 个，当前提供了 {len(parsed_tags)} 个: {parsed_tags}", file=sys.stderr)
            return 1
        request = XiaohongshuVideoUploadRequest(
            account_name=args.account,
            video_file=args.file,
            title=args.title,
            description=args.desc,
            tags=parsed_tags,
            publish_date=args.schedule or 0,
            thumbnail_file=args.thumbnail,
            publish_strategy=publish_strategy,
            debug=args.debug,
            headless=args.headless,
            prepare_only=safety_options["prepare_only"],
            result_file=safety_options["result_file"],
            request_id=safety_options["request_id"],
            timeout=safety_options["timeout"],
        )
        try:
            await upload_xiaohongshu_video(request)
        except PublishAlreadySubmitted as exc:
            print(f"Xiaohongshu video already submitted (request-id={request.request_id}), skipped: {exc}")
            return 0
        if request.prepare_only:
            print(f"Xiaohongshu video prepared (not published): {request.video_file}")
        else:
            print(f"Xiaohongshu video upload submitted: {request.video_file}")
        return 0

    if args.action == "upload-note":
        safety_options = publish_safety_options(args)
        parsed_tags = parse_tags(args.tags)
        if len(parsed_tags) > 10:
            print(f"错误：小红书标签最多 10 个，当前提供了 {len(parsed_tags)} 个: {parsed_tags}", file=sys.stderr)
            return 1
        request = XiaohongshuNoteUploadRequest(
            account_name=args.account,
            image_files=parse_image_files(args.images),
            title=args.title,
            note=args.note,
            tags=parsed_tags,
            publish_date=args.schedule or 0,
            publish_strategy=publish_strategy,
            debug=args.debug,
            headless=args.headless,
            prepare_only=safety_options["prepare_only"],
            result_file=safety_options["result_file"],
            request_id=safety_options["request_id"],
            timeout=safety_options["timeout"],
        )
        try:
            await upload_xiaohongshu_note(request)
        except PublishAlreadySubmitted as exc:
            print(f"Xiaohongshu note already submitted (request-id={request.request_id}), skipped: {exc}")
            return 0
        if request.prepare_only:
            print(f"Xiaohongshu note prepared (not published): {len(request.image_files)} images")
        else:
            print(f"Xiaohongshu note upload submitted: {len(request.image_files)} images")
        return 0

    raise RuntimeError(f"Unsupported Xiaohongshu action: {args.action}")
