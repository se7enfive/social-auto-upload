"""douyin 的命令参数与执行逻辑。"""
from __future__ import annotations
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinNote,
    DouYinVideo,
    cookie_auth as douyin_cookie_auth,
    douyin_setup,
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
from utils.publish_pacing import enforce_publish_pacing


def register(platform_parsers):
    """声明本平台命令、参数及合法选项。"""
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    douyin_parser = platform_parsers.add_parser("douyin", help="Douyin operations")
    douyin_actions = douyin_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = douyin_actions.add_parser(action_name, help=f"Douyin {action_name}")
        action_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    upload_video_parser = douyin_actions.add_parser("upload-video", help="Upload one video to Douyin")
    upload_video_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
    upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    upload_video_parser.add_argument("--title", required=True, help="Video title")
    upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    upload_video_parser.add_argument("--thumbnail-landscape", type=existing_file_path, help="Optional 4:3 landscape thumbnail path")
    upload_video_parser.add_argument("--thumbnail-portrait", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    upload_video_parser.add_argument("--product-link", default="", help="Optional product link")
    upload_video_parser.add_argument("--product-title", default="", help="Optional product title")
    upload_video_parser.add_argument(
        "--declaration",
        help="Exact Douyin self-declaration option text; omitted means do not set one",
    )
    upload_video_parser.add_argument("--collection", default=None, help="Optional collection name to add the work into (must already exist)")
    add_publish_safety_flags(upload_video_parser)
    add_runtime_flags(upload_video_parser)

    upload_note_parser = douyin_actions.add_parser("upload-note", help="Upload one note to Douyin")
    upload_note_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
    upload_note_parser.add_argument("--images", required=True, nargs="+", type=existing_file_path, help="Image file paths")
    upload_note_parser.add_argument("--title", required=True, help="Note title")
    upload_note_parser.add_argument("--note", default="", help="Optional note content")
    upload_note_parser.add_argument("--notef", default="", help="Read note content from file (txt/md)")
    upload_note_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    upload_note_parser.add_argument("--bgm", default="", help="BGM music name to search and select")
    upload_note_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    add_publish_safety_flags(upload_note_parser)
    add_runtime_flags(upload_note_parser)


@dataclass(slots=True)
class DouyinVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    product_link: str = ""
    product_title: str = ""
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    declaration: str | None = None
    collection_name: str | None = None
    prepare_only: bool = False
    result_file: Path | None = None
    request_id: str | None = None
    timeout: float = DEFAULT_PUBLISH_TIMEOUT


@dataclass(slots=True)
class DouyinNoteUploadRequest:
    account_name: str
    image_files: list[Path]
    title: str
    note: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    bgm: str = ""
    prepare_only: bool = False
    result_file: Path | None = None
    request_id: str | None = None
    timeout: float = DEFAULT_PUBLISH_TIMEOUT


async def login_douyin_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("douyin", account_name)
    with session_lock(account_file, "douyin", account_name):
        return await douyin_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_douyin_account(account_name: str) -> bool:
    account_file = resolve_account_file("douyin", account_name)
    with session_lock(account_file, "douyin", account_name):
        if not account_file.exists():
            return False
        return await douyin_cookie_auth(str(account_file))


def _schedule_fingerprint(publish_date: datetime | int) -> str | int:
    return publish_date.isoformat() if isinstance(publish_date, datetime) else 0


def _video_fingerprint(request: DouyinVideoUploadRequest) -> str:
    return content_fingerprint(
        {
            "platform": "douyin",
            "kind": "video",
            "title": request.title,
            "description": request.description,
            "tags": request.tags,
            "publish_date": _schedule_fingerprint(request.publish_date),
            "media": [file_signature(request.video_file)],
            "thumbnails": [
                file_signature(path)
                for path in (
                    request.thumbnail_file,
                    request.thumbnail_landscape_file,
                    request.thumbnail_portrait_file,
                )
                if path
            ],
            "product_link": request.product_link,
            "product_title": request.product_title,
            "declaration": request.declaration,
            "collection": request.collection_name,
        }
    )


def _note_fingerprint(request: DouyinNoteUploadRequest) -> str:
    return content_fingerprint(
        {
            "platform": "douyin",
            "kind": "note",
            "title": request.title,
            "note": request.note,
            "tags": request.tags,
            "publish_date": _schedule_fingerprint(request.publish_date),
            "media": [file_signature(path) for path in request.image_files],
            "bgm": request.bgm,
        }
    )


async def upload_video(request: DouyinVideoUploadRequest) -> Path:
    account_file = resolve_account_file("douyin", request.account_name)
    fingerprint = _video_fingerprint(request)
    with session_lock(account_file, "douyin", request.account_name):
        attempt = PublicationAttempt(
            platform="douyin",
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
        enforce_publish_pacing(attempt)
        attempt.begin()
        try:
            is_ready = await douyin_setup(str(account_file), handle=False)
            if not is_ready:
                raise RuntimeError(
                    f"Douyin cookie is missing or expired: {account_file}. Run `sau douyin login --account {request.account_name}` first."
                )

            app = DouYinVideo(
                request.title,
                str(request.video_file),
                request.tags,
                request.publish_date,
                str(account_file),
                desc=request.description,
                thumbnail_landscape_path=(
                    str(request.thumbnail_landscape_file) if request.thumbnail_landscape_file else None
                ),
                thumbnail_portrait_path=str(
                    request.thumbnail_portrait_file or request.thumbnail_file
                ) if request.thumbnail_portrait_file or request.thumbnail_file else None,
                productLink=request.product_link,
                productTitle=request.product_title,
                declaration=request.declaration,
                publish_strategy=request.publish_strategy,
                debug=request.debug,
                headless=request.headless,
                collection_name=request.collection_name,
                safety=attempt,
            )
            await attempt.run_with_deadline(app.douyin_upload_video())
        except Exception as exc:
            attempt.finalize_on_error(str(exc))
            raise
        finally:
            # 失败时也导出最终记录，便于调用方读取真实状态；导出失败不影响原始结果。
            attempt.export(request.result_file)
    return account_file


async def upload_note(request: DouyinNoteUploadRequest) -> Path:
    account_file = resolve_account_file("douyin", request.account_name)
    fingerprint = _note_fingerprint(request)
    with session_lock(account_file, "douyin", request.account_name):
        attempt = PublicationAttempt(
            platform="douyin",
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
        enforce_publish_pacing(attempt)
        attempt.begin()
        try:
            is_ready = await douyin_setup(str(account_file), handle=False)
            if not is_ready:
                raise RuntimeError(
                    f"Douyin cookie is missing or expired: {account_file}. Run `sau douyin login --account {request.account_name}` first."
                )

            app = DouYinNote(
                image_paths=[str(path) for path in request.image_files],
                title=request.title,
                note=request.note,
                tags=request.tags,
                publish_date=request.publish_date,
                account_file=str(account_file),
                publish_strategy=request.publish_strategy,
                debug=request.debug,
                headless=request.headless,
                bgm=request.bgm,
                safety=attempt,
            )
            await attempt.run_with_deadline(app.douyin_upload_note())
        except Exception as exc:
            attempt.finalize_on_error(str(exc))
            raise
        finally:
            attempt.export(request.result_file)
    return account_file


async def run(args):
    """执行本平台命令；发布结果由平台实现核验。"""
    if args.action == "login":
        result = await login_douyin_account(args.account, headless=args.headless)
        if not result["success"]:
            raise RuntimeError(result["message"])
        print(f"Douyin login flow completed: {result['account_file']}")
        return 0

    if args.action == "check":
        is_valid = await check_douyin_account(args.account)
        print("valid" if is_valid else "invalid")
        return 0 if is_valid else 1

    publish_strategy = DOUYIN_PUBLISH_STRATEGY_SCHEDULED if args.schedule else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE

    if args.action == "upload-video":
        safety_options = publish_safety_options(args)
        request = DouyinVideoUploadRequest(
            account_name=args.account,
            video_file=args.file,
            title=args.title,
            description=args.desc,
            tags=parse_tags(args.tags),
            publish_date=args.schedule or 0,
            thumbnail_file=args.thumbnail,
            thumbnail_landscape_file=args.thumbnail_landscape,
            thumbnail_portrait_file=args.thumbnail_portrait,
            product_link=args.product_link,
            product_title=args.product_title,
            declaration=args.declaration,
            publish_strategy=publish_strategy,
            debug=args.debug,
            headless=args.headless,
            collection_name=args.collection,
            prepare_only=safety_options["prepare_only"],
            result_file=safety_options["result_file"],
            request_id=safety_options["request_id"],
            timeout=safety_options["timeout"],
        )
        try:
            await upload_video(request)
        except PublishAlreadySubmitted as exc:
            print(f"Douyin video already submitted (request-id={request.request_id}), skipped: {exc}")
            return 0
        if request.prepare_only:
            print(f"Douyin video prepared (not published): {request.video_file}")
        else:
            print(f"Douyin video upload submitted: {request.video_file}")
        return 0

    if args.action == "upload-note":
        safety_options = publish_safety_options(args)
        # 如果指定了 --notef，读取文件内容作为 note
        note_content = args.note
        if args.notef:
            note_file = Path(args.notef)
            if not note_file.exists():
                print(f"错误：文件不存在: {note_file}", file=sys.stderr)
                return 1
            note_content = note_file.read_text(encoding="utf-8")

        request = DouyinNoteUploadRequest(
            account_name=args.account,
            image_files=parse_image_files(args.images),
            title=args.title,
            note=note_content,
            tags=parse_tags(args.tags),
            publish_date=args.schedule or 0,
            publish_strategy=publish_strategy,
            debug=args.debug,
            headless=args.headless,
            bgm=args.bgm or "",
            prepare_only=safety_options["prepare_only"],
            result_file=safety_options["result_file"],
            request_id=safety_options["request_id"],
            timeout=safety_options["timeout"],
        )
        try:
            await upload_note(request)
        except PublishAlreadySubmitted as exc:
            print(f"Douyin note already submitted (request-id={request.request_id}), skipped: {exc}")
            return 0
        if request.prepare_only:
            print(f"Douyin note prepared (not published): {len(request.image_files)} images")
        else:
            print(f"Douyin note upload submitted: {len(request.image_files)} images")
        return 0

    raise RuntimeError(f"Unsupported Douyin action: {args.action}")
