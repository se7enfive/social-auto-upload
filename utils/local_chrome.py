"""本机 Chrome 持久化 profile 的启动、登录态导入与备份导出。

抖音、小红书、视频号只通过本模块启动浏览器：始终使用本机安装的 Google Chrome，
始终有头，登录态保存在每个账号独立的 Chrome profile 中。cookies/*.json 仅用于
首次导入和备份，不再是登录态的唯一来源。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import conf

from utils.base_social_media import set_init_script
from utils.publish_safety import state_root

logger = logging.getLogger(__name__)

IMPORT_MARKER_NAME = ".sau-storage-import.json"

# 各平台登录态相关的 cookie 域后缀；未知平台导入全部 cookie。
_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin.com",),
    "xiaohongshu": ("xiaohongshu.com",),
    "tencent": ("weixin.qq.com", "qq.com"),
}

_SAME_SITE_VALUES = {"lax": "Lax", "strict": "Strict", "none": "None"}

_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-search-engine-choice-screen",
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--lang=zh-CN",
]

_PROFILE_BUSY_TOKENS = (
    "processsingleton",
    "singletonlock",
    "singleton",
    "already in use",
    "already in use by another",
    "existing browser session",
)

_CHROME_MISSING_TOKENS = (
    "executable doesn't exist",
    "is not found at",
    "distribution 'chrome' is not found",
    "distribution \"chrome\" is not found",
    "no such file",
)


class LocalChromeError(RuntimeError):
    """本机 Chrome 启动或登录态处理失败。"""


class LocalChromeUnavailable(LocalChromeError):
    """找不到可用的本机 Google Chrome。"""


class LocalChromeProfileBusy(LocalChromeError):
    """该账号的专用 Chrome profile 正被其它进程占用。"""


def _safe_component(value: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")[:max_len]
    return slug or "account"


def profile_root() -> Path:
    """专用 Chrome profile 根目录；默认 .local/chrome-profiles。"""
    override = str(getattr(conf, "LOCAL_CHROME_PROFILE_ROOT", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return state_root() / "chrome-profiles"


def profile_dir_for(platform: str, account_file: str | Path) -> Path:
    """每个平台、每个账号一个稳定且互不冲突的 profile 目录。"""
    path = Path(account_file).expanduser()
    normalized = os.path.normcase(str(path.resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return profile_root() / _safe_component(platform) / f"{_safe_component(path.stem)}-{digest}"


def has_local_session(platform: str, account_file: str | Path) -> bool:
    """本地是否已有可复用的登录态：账号 JSON 或已初始化的 profile。"""
    try:
        if Path(account_file).expanduser().is_file():
            return True
        profile_dir = profile_dir_for(platform, account_file)
        return (profile_dir / "Default").is_dir()
    except OSError:
        return False


def ensure_headed(platform: str, headless: bool) -> None:
    """这三个平台固定有头运行，显式传入无头直接拒绝。"""
    if headless:
        raise LocalChromeError(
            f"{platform} 只支持本机 Chrome 有头模式，不能使用无头模式或 Chromium。"
            " 请去掉 --headless；登录、检查和发布都会打开可见的 Chrome 窗口。"
        )


def _configured_chrome_path() -> str:
    return str(getattr(conf, "LOCAL_CHROME_PATH", "") or "").strip()


def build_launch_options() -> tuple[dict, str | None]:
    """返回 launch_persistent_context 的公共参数；不包含 user_data_dir。"""
    options: dict = {
        "headless": False,
        "no_viewport": True,
        "args": list(_LAUNCH_ARGS),
        "timeout": 60_000,
    }
    warning = None
    configured = _configured_chrome_path()
    if configured:
        executable = Path(configured).expanduser()
        if executable.is_file():
            options["executable_path"] = str(executable)
        else:
            warning = (
                f"LOCAL_CHROME_PATH 指向的文件不存在：{configured}；改用系统安装的 Google Chrome。"
            )
    if "executable_path" not in options:
        options["channel"] = "chrome"
    return options, warning


def _is_profile_busy(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in _PROFILE_BUSY_TOKENS)


def _profile_lock_present(profile_dir: Path) -> bool:
    """启动失败时 Windows Chrome 会留下 lockfile；POSIX 使用 SingletonLock。"""
    try:
        return (profile_dir / "lockfile").exists() or (profile_dir / "SingletonLock").exists()
    except OSError:
        return False


def _first_error_line(message: str) -> str:
    stripped = message.strip()
    if not stripped:
        return "(无错误详情)"
    return stripped.splitlines()[0][:500]


def _is_chrome_missing(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in _CHROME_MISSING_TOKENS)


def _unavailable_message(message: str) -> str:
    configured = _configured_chrome_path() or "(未设置)"
    return (
        "无法启动本机 Google Chrome。"
        f" LOCAL_CHROME_PATH={configured}。"
        " 请安装 Google Chrome，或在 conf.py 中把 LOCAL_CHROME_PATH 指向 chrome.exe。"
        " 抖音、小红书、视频号不支持 Chromium、无头模式或 Docker 环境。"
        f" 原始错误：{message}"
    )


async def launch_local_chrome(
    playwright,
    platform: str,
    account_file: str | Path,
    *,
    permissions: list[str] | None = None,
):
    """以持久化、有头方式启动本机 Chrome，并返回 BrowserContext。

    调用方只能通过 ``context.close()``（推荐 ``close_browser_resources(context, None)``）
    结束会话；context 关闭时 Chrome 随之退出。
    """
    profile_dir = profile_dir_for(platform, account_file)
    profile_dir.mkdir(parents=True, exist_ok=True)

    options, warning = build_launch_options()
    if warning:
        logger.warning(warning)
    if permissions:
        options["permissions"] = list(permissions)

    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **options,
        )
    except Exception as exc:
        message = str(exc)
        if _is_chrome_missing(message):
            raise LocalChromeUnavailable(_unavailable_message(message)) from exc
        if _is_profile_busy(message) or _profile_lock_present(profile_dir):
            raise LocalChromeProfileBusy(
                f"{platform} 的专用 Chrome profile 正被占用：{profile_dir}。"
                " 请先关闭该账号已打开的 Chrome 窗口，并避免同一账号并发运行。"
                f" 原始错误：{_first_error_line(message)}"
            ) from exc
        raise LocalChromeError(
            f"{platform} 启动本机 Chrome 失败（profile：{profile_dir}）：{message}"
        ) from exc

    try:
        context = await set_init_script(context)
        await import_storage_state_if_needed(context, profile_dir, account_file, platform=platform)
        return context
    except Exception:
        try:
            await context.close()
        except Exception:
            logger.debug("关闭启动失败的本机 Chrome context 时出错", exc_info=True)
        raise


def _target_domain_suffixes(platform: str | None) -> tuple[str, ...] | None:
    if not platform:
        return None
    return _DOMAIN_SUFFIXES.get(platform)


def _domain_matches(domain: str, suffixes: tuple[str, ...] | None) -> bool:
    if not suffixes:
        return True
    cleaned = str(domain or "").lstrip(".").lower()
    return any(cleaned == suffix or cleaned.endswith("." + suffix) for suffix in suffixes)


def _read_storage_state(account_file: str | Path) -> dict | None:
    path = Path(account_file)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("账号 JSON 无法读取，跳过导入（可重新登录）：%s（%s）", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("账号 JSON 结构不正确，跳过导入（可重新登录）：%s", path)
        return None
    return data


def _normalize_cookie(cookie: dict, suffixes: tuple[str, ...] | None) -> dict | None:
    name = cookie.get("name")
    value = cookie.get("value")
    domain = cookie.get("domain")
    if not name or value is None:
        return None
    if not domain:
        return None
    if not _domain_matches(str(domain), suffixes):
        return None

    expires = cookie.get("expires", -1)
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        if expires > 0 and expires < time.time():
            return None
    else:
        expires = -1

    item: dict = {
        "name": str(name),
        "value": str(value),
        "domain": str(domain),
        "path": str(cookie.get("path") or "/"),
        "expires": float(expires),
    }
    if "httpOnly" in cookie:
        item["httpOnly"] = bool(cookie["httpOnly"])
    if "secure" in cookie:
        item["secure"] = bool(cookie["secure"])

    same_site = cookie.get("sameSite")
    if same_site:
        normalized = _SAME_SITE_VALUES.get(str(same_site).lower())
        if normalized:
            item["sameSite"] = normalized
    return item


async def _add_cookies(context, cookies: list[dict]) -> int:
    if not cookies:
        return 0
    try:
        await context.add_cookies(cookies)
        return len(cookies)
    except Exception as exc:
        logger.warning("批量导入 cookie 失败，改为逐条导入：%s", exc)

    imported = 0
    for cookie in cookies:
        try:
            await context.add_cookies([cookie])
            imported += 1
        except Exception as exc:
            logger.warning("跳过无法导入的 cookie %s：%s", cookie.get("name"), exc)
    return imported


def _local_storage_entries(origins, suffixes: tuple[str, ...] | None) -> list[tuple[str, list[list[str]]]]:
    entries: list[tuple[str, list[list[str]]]] = []
    if not isinstance(origins, list):
        return entries
    for entry in origins:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        items = entry.get("localStorage")
        if not origin or not isinstance(items, list):
            continue
        hostname = urlparse(str(origin)).hostname or ""
        if suffixes and not _domain_matches(hostname, suffixes):
            continue
        pairs = [
            [str(item["name"]), str(item["value"])]
            for item in items
            if isinstance(item, dict) and item.get("name") is not None and item.get("value") is not None
        ]
        if pairs:
            entries.append((str(origin), pairs))
    return entries


async def _import_local_storage(context, origins, suffixes: tuple[str, ...] | None) -> int:
    entries = _local_storage_entries(origins, suffixes)
    if not entries:
        return 0

    parts = []
    for index, (origin, pairs) in enumerate(entries):
        parts.append(
            f"""
(() => {{
  if (location.origin !== {json.dumps(origin)}) return;
  const flag = "__sau_storage_imported_{index}";
  if (sessionStorage.getItem(flag)) return;
  sessionStorage.setItem(flag, "1");
  const items = {json.dumps(pairs, ensure_ascii=True)};
  for (const item of items) {{
    try {{ localStorage.setItem(item[0], item[1]); }} catch (error) {{}}
  }}
}})();
"""
        )
    await context.add_init_script(script="\n".join(parts))
    return len(entries)


def _import_enabled() -> bool:
    return bool(getattr(conf, "LOCAL_CHROME_IMPORT_STORAGE_STATE", True))


def _write_import_marker(marker: Path, account_file: str | Path, cookies: int, origins: int) -> None:
    payload = {
        "source": Path(account_file).name,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "cookies": cookies,
        "origins": origins,
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("写入登录态导入标记失败：%s（%s）", marker, exc)


async def import_storage_state_if_needed(
    context,
    profile_dir: str | Path,
    account_file: str | Path,
    *,
    platform: str | None = None,
) -> dict:
    """首次启动时把旧 storage_state 导入 profile；之后不再重复。

    返回导入数量摘要。JSON 缺失、损坏或部分 cookie 失败都不阻止启动。
    """
    summary = {"enabled": _import_enabled(), "imported": False, "cookies": 0, "origins": 0}
    if not summary["enabled"]:
        return summary

    marker = Path(profile_dir) / IMPORT_MARKER_NAME
    if marker.exists():
        return summary

    suffixes = _target_domain_suffixes(platform)
    try:
        existing = await context.cookies()
    except Exception as exc:
        logger.debug("读取 profile 现有 cookie 失败：%s", exc)
        existing = []
    if any(_domain_matches(cookie.get("domain", ""), suffixes) for cookie in existing):
        # profile 已经登录，旧 JSON 不应覆盖更新的会话。
        return summary

    data = _read_storage_state(account_file)
    if data is None:
        return summary

    cookie_count = await _add_cookies(
        context,
        [
            item
            for item in (
                _normalize_cookie(cookie, suffixes)
                for cookie in (data.get("cookies") or [])
                if isinstance(cookie, dict)
            )
            if item is not None
        ],
    )
    origin_count = await _import_local_storage(context, data.get("origins") or [], suffixes)

    _write_import_marker(marker, account_file, cookie_count, origin_count)
    summary.update(imported=True, cookies=cookie_count, origins=origin_count)
    logger.info(
        "已从账号 JSON 导入登录态到专用 Chrome profile：cookie %s 条、origin %s 个",
        cookie_count,
        origin_count,
    )
    return summary


async def export_storage_state(context, account_file: str | Path) -> bool:
    """把当前 profile 的登录态写回账号 JSON，作为导入/回滚备份。

    先写临时文件再原子替换；失败只警告，不影响发布结果。
    """
    path = Path(account_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("创建账号备份目录失败：%s（%s）", path.parent, exc)
        return False

    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        await context.storage_state(path=str(temp_path))
        os.replace(temp_path, path)
        return True
    except Exception as exc:
        logger.warning("导出账号备份失败（不影响发布结果）：%s（%s）", path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
