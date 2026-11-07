# Language: 中文
"""静态防回归：三个平台的生产源文件只能通过本机 Chrome 模块启动浏览器。"""
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TARGET_SOURCES = (
    PROJECT_ROOT / "uploader" / "douyin_uploader" / "main.py",
    PROJECT_ROOT / "uploader" / "xiaohongshu_uploader" / "main.py",
    PROJECT_ROOT / "uploader" / "tencent_uploader" / "main.py",
)

FORBIDDEN_PATTERNS = (
    'channel="chromium"',
    "channel='chromium'",
    "playwright.chromium.launch(",
    "headless=True",
    "storage_state=",
    "set_init_script",
    "LOCAL_CHROME_PATH",
)


class LocalChromeSourceGuardTests(unittest.TestCase):
    def test_target_uploaders_only_use_local_chrome(self):
        for path in TARGET_SOURCES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(source=str(path.relative_to(PROJECT_ROOT))):
                for pattern in FORBIDDEN_PATTERNS:
                    self.assertNotIn(
                        pattern,
                        source,
                        f"{path.name} 仍包含禁止的浏览器调用片段: {pattern}",
                    )
                self.assertIn("from utils.local_chrome import", source)
                self.assertIn("launch_local_chrome(", source)
                self.assertIn("export_storage_state(", source)
                self.assertIn("has_local_session(", source)
                self.assertIn("ensure_headed(", source)

    def test_target_uploaders_never_call_chromium_launch(self):
        for path in TARGET_SOURCES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(source=str(path.relative_to(PROJECT_ROOT))):
                self.assertNotIn("chromium.launch", source)
                self.assertNotIn("connect_over_cdp(", source)


if __name__ == "__main__":
    unittest.main()
