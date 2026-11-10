from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
LOCAL_CHROME_HEADLESS = True  # only legacy platforms; douyin/xiaohongshu/tencent always run headed
# Dedicated per-account Chrome profiles for douyin/xiaohongshu/tencent.
# Empty = .local/chrome-profiles (or $SAU_STATE_DIR/chrome-profiles).
LOCAL_CHROME_PROFILE_ROOT = ""
# Import cookies/{platform}_{account}.json into a new profile once; JSON is still refreshed as a backup.
LOCAL_CHROME_IMPORT_STORAGE_STATE = True
# Douyin HTTP publish engine (external DouYin_Spider runtime).
# Empty DOUYIN_HTTP_ROOT = ~/.social-auto-upload/tools/douyin-http (or $SAU_DOUYIN_HTTP_ROOT).
DOUYIN_HTTP_ROOT = ""
# auto | browser | http; auto prefers HTTP when the runtime and credentials exist.
DOUYIN_UPLOAD_ENGINE = "auto"
DEBUG_MODE = True  # default debug behavior
# Optional proxy for the YouTube uploader. Where youtube.com is blocked, direct
# connections time out and the (patchright) chromium does NOT use the system proxy.
# Point this at your local proxy port, e.g. "http://127.0.0.1:7890". None = no proxy.
YT_PROXY = None
