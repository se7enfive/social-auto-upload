# Language: 中文
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import conf
from utils import local_chrome
from utils.local_chrome import (
    IMPORT_MARKER_NAME,
    LocalChromeError,
    LocalChromeProfileBusy,
    LocalChromeUnavailable,
    build_launch_options,
    ensure_headed,
    export_storage_state,
    has_local_session,
    import_storage_state_if_needed,
    launch_local_chrome,
    profile_dir_for,
    profile_root,
)


class ChromeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._patches = [
            patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")}),
            patch.object(conf, "LOCAL_CHROME_PROFILE_ROOT", "", create=True),
            patch.object(conf, "LOCAL_CHROME_PATH", "", create=True),
            patch.object(conf, "LOCAL_CHROME_IMPORT_STORAGE_STATE", True, create=True),
        ]
        for patcher in self._patches:
            patcher.start()
        self.account_file = self.tmp / "cookies" / "douyin_main.json"

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self._tmp.cleanup()

    def write_account(self, data) -> None:
        self.account_file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            self.account_file.write_text(data, encoding="utf-8")
        else:
            self.account_file.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def fake_context() -> MagicMock:
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])
        context.add_cookies = AsyncMock()
        context.add_init_script = AsyncMock()
        context.close = AsyncMock()
        context.storage_state = AsyncMock()
        return context


class ProfilePathTests(ChromeTestCase):
    def test_profile_root_defaults_to_state_dir(self):
        self.assertEqual(profile_root(), self.tmp / "state" / "chrome-profiles")

    def test_profile_root_override(self):
        with patch.object(conf, "LOCAL_CHROME_PROFILE_ROOT", str(self.tmp / "custom")):
            self.assertEqual(profile_root(), self.tmp / "custom")

    def test_profile_dir_is_stable_for_same_account(self):
        first = profile_dir_for("douyin", self.account_file)
        second = profile_dir_for("douyin", self.account_file)
        self.assertEqual(first, second)
        self.assertEqual(first.parent.name, "douyin")
        self.assertIn("douyin_main-", first.name)

    def test_profile_dir_differs_for_same_file_stem(self):
        other = self.tmp / "another" / "cookies" / "douyin_main.json"
        self.assertNotEqual(
            profile_dir_for("douyin", self.account_file),
            profile_dir_for("douyin", other),
        )

    def test_profile_dir_differs_by_platform(self):
        self.assertNotEqual(
            profile_dir_for("douyin", self.account_file),
            profile_dir_for("xiaohongshu", self.account_file),
        )

    def test_profile_dir_cannot_escape_root(self):
        result = profile_dir_for("../../evil", self.account_file)
        self.assertEqual(result.parent.parent, profile_root())

    def test_has_local_session_without_files(self):
        self.assertFalse(has_local_session("douyin", self.account_file))

    def test_has_local_session_with_account_json(self):
        self.write_account({"cookies": [], "origins": []})
        self.assertTrue(has_local_session("douyin", self.account_file))

    def test_has_local_session_with_initialized_profile(self):
        profile = profile_dir_for("douyin", self.account_file)
        (profile / "Default").mkdir(parents=True)
        self.assertTrue(has_local_session("douyin", self.account_file))

    def test_has_local_session_ignores_empty_profile_dir(self):
        profile_dir_for("douyin", self.account_file).mkdir(parents=True)
        self.assertFalse(has_local_session("douyin", self.account_file))


class LaunchOptionTests(ChromeTestCase):
    def test_existing_local_chrome_path_is_preferred(self):
        chrome = self.tmp / "chrome.exe"
        chrome.write_bytes(b"fake")
        with patch.object(conf, "LOCAL_CHROME_PATH", str(chrome)):
            options, warning = build_launch_options()
        self.assertEqual(options["executable_path"], str(chrome))
        self.assertNotIn("channel", options)
        self.assertIsNone(warning)
        self.assertFalse(options["headless"])
        self.assertTrue(options["no_viewport"])
        self.assertIn("--start-maximized", options["args"])

    def test_missing_local_chrome_path_falls_back_to_channel(self):
        with patch.object(conf, "LOCAL_CHROME_PATH", str(self.tmp / "missing.exe")):
            options, warning = build_launch_options()
        self.assertNotIn("executable_path", options)
        self.assertEqual(options["channel"], "chrome")
        self.assertIn("不存在", warning)

    def test_empty_local_chrome_path_uses_channel(self):
        options, warning = build_launch_options()
        self.assertEqual(options["channel"], "chrome")
        self.assertIsNone(warning)

    def test_launch_options_never_use_chromium(self):
        options, _ = build_launch_options()
        self.assertNotEqual(options.get("channel"), "chromium")
        self.assertNotEqual(options.get("executable_path"), "chromium")

    def test_ensure_headed_rejects_headless(self):
        with self.assertRaises(LocalChromeError) as caught:
            ensure_headed("douyin", True)
        self.assertIn("有头", str(caught.exception))

    def test_ensure_headed_allows_headed(self):
        ensure_headed("douyin", False)


class LaunchTests(ChromeTestCase):
    def setUp(self):
        super().setUp()
        self.context = self.fake_context()
        self.playwright = MagicMock()
        self.playwright.chromium.launch_persistent_context = AsyncMock(return_value=self.context)
        self.playwright.chromium.launch = AsyncMock(side_effect=AssertionError("不得调用 launch()"))
        self.init_script_patcher = patch.object(
            local_chrome, "set_init_script", AsyncMock(side_effect=lambda context: context)
        )
        self.init_script_patcher.start()

    def tearDown(self):
        self.init_script_patcher.stop()
        super().tearDown()

    def test_launch_uses_persistent_context_once(self):
        async def run():
            return await launch_local_chrome(self.playwright, "douyin", self.account_file)

        context = asyncio.run(run())

        self.assertIs(context, self.context)
        self.playwright.chromium.launch_persistent_context.assert_awaited_once()
        self.playwright.chromium.launch.assert_not_called()
        kwargs = self.playwright.chromium.launch_persistent_context.await_args.kwargs
        self.assertEqual(kwargs["user_data_dir"], str(profile_dir_for("douyin", self.account_file)))
        self.assertFalse(kwargs["headless"])
        self.assertTrue(kwargs["no_viewport"])
        self.assertEqual(kwargs["channel"], "chrome")

    def test_launch_passes_permissions(self):
        async def run():
            await launch_local_chrome(
                self.playwright, "douyin", self.account_file, permissions=["notifications"]
            )

        asyncio.run(run())
        kwargs = self.playwright.chromium.launch_persistent_context.await_args.kwargs
        self.assertEqual(kwargs["permissions"], ["notifications"])

    def test_busy_profile_raises_clear_error(self):
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError(
            "Failed to create a ProcessSingleton for your profile directory.\n"
            "Browser logs:\n<launching> chrome.exe --user-data-dir=..."
        )

        async def run():
            await launch_local_chrome(self.playwright, "douyin", self.account_file)

        with self.assertRaises(LocalChromeProfileBusy) as caught:
            asyncio.run(run())
        message = str(caught.exception)
        self.assertIn("占用", message)
        self.assertIn("ProcessSingleton", message)
        self.assertNotIn("Browser logs", message)

    def test_profile_lockfile_marks_generic_failure_as_busy(self):
        profile = profile_dir_for("douyin", self.account_file)
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "lockfile").write_text("", encoding="utf-8")
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError(
            "BrowserType.launch_persistent_context: Target page, context or browser has been closed"
        )

        async def run():
            await launch_local_chrome(self.playwright, "douyin", self.account_file)

        with self.assertRaises(LocalChromeProfileBusy) as caught:
            asyncio.run(run())
        self.assertIn("占用", str(caught.exception))

    def test_missing_chrome_raises_unavailable(self):
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError(
            "Chromium distribution 'chrome' is not found at /opt/google/chrome"
        )

        async def run():
            await launch_local_chrome(self.playwright, "douyin", self.account_file)

        with self.assertRaises(LocalChromeUnavailable) as caught:
            asyncio.run(run())
        message = str(caught.exception)
        self.assertIn("LOCAL_CHROME_PATH", message)
        self.assertIn("不支持 Chromium", message)

    def test_other_launch_error_is_wrapped(self):
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError("boom")

        async def run():
            await launch_local_chrome(self.playwright, "douyin", self.account_file)

        with self.assertRaises(LocalChromeError) as caught:
            asyncio.run(run())
        self.assertNotIsInstance(caught.exception, (LocalChromeProfileBusy, LocalChromeUnavailable))

    def test_context_closed_when_setup_fails(self):
        local_chrome.set_init_script.side_effect = RuntimeError("stealth failed")

        async def run():
            await launch_local_chrome(self.playwright, "douyin", self.account_file)

        with self.assertRaises(RuntimeError):
            asyncio.run(run())
        self.context.close.assert_awaited_once()


class ImportTests(ChromeTestCase):
    def account_data(self) -> dict:
        now = time.time()
        return {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "secret-cookie-value",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": now + 3600,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "lax",
                },
                {
                    "name": "expired",
                    "value": "old",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": now - 10,
                },
                {
                    "name": "other",
                    "value": "other-value",
                    "domain": ".example.com",
                    "path": "/",
                    "expires": now + 3600,
                },
            ],
            "origins": [
                {
                    "origin": "https://www.douyin.com",
                    "localStorage": [{"name": "theme", "value": "dark"}],
                },
                {
                    "origin": "https://example.com",
                    "localStorage": [{"name": "leak", "value": "leak-value"}],
                },
            ],
        }

    def test_imports_matching_cookies_and_local_storage(self):
        data = self.account_data()
        self.write_account(data)
        context = self.fake_context()
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )

        self.assertTrue(summary["imported"])
        self.assertEqual(summary["cookies"], 1)
        self.assertEqual(summary["origins"], 1)
        cookies = context.add_cookies.await_args.args[0]
        self.assertEqual([cookie["name"] for cookie in cookies], ["sessionid"])
        self.assertEqual(cookies[0]["sameSite"], "Lax")
        self.assertEqual(cookies[0]["expires"], data["cookies"][0]["expires"])

        script = context.add_init_script.await_args.kwargs["script"]
        self.assertIn("https://www.douyin.com", script)
        self.assertNotIn("example.com", script)
        self.assertIn("sessionStorage", script)
        self.assertIn("location.origin !==", script)

        marker = profile / IMPORT_MARKER_NAME
        self.assertTrue(marker.is_file())
        marker_text = marker.read_text(encoding="utf-8")
        self.assertNotIn("secret-cookie-value", marker_text)
        self.assertNotIn("theme", marker_text)

    def test_marker_prevents_repeated_import(self):
        self.write_account(self.account_data())
        context = self.fake_context()
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        asyncio.run(import_storage_state_if_needed(context, profile, self.account_file, platform="douyin"))
        context.add_cookies.reset_mock()
        context.add_init_script.reset_mock()
        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )

        self.assertFalse(summary["imported"])
        context.add_cookies.assert_not_called()
        context.add_init_script.assert_not_called()

    def test_existing_profile_cookie_blocks_import(self):
        self.write_account(self.account_data())
        context = self.fake_context()
        context.cookies = AsyncMock(return_value=[{"domain": ".douyin.com", "name": "sessionid"}])
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )

        self.assertFalse(summary["imported"])
        context.add_cookies.assert_not_called()
        self.assertFalse((profile / IMPORT_MARKER_NAME).exists())

    def test_corrupt_json_does_not_block_or_mark(self):
        self.write_account("{not json")
        context = self.fake_context()
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )

        self.assertFalse(summary["imported"])
        context.add_cookies.assert_not_called()
        self.assertFalse((profile / IMPORT_MARKER_NAME).exists())

    def test_missing_json_does_nothing(self):
        context = self.fake_context()
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )

        self.assertFalse(summary["imported"])
        self.assertFalse((profile / IMPORT_MARKER_NAME).exists())

    def test_disabled_import_does_nothing(self):
        self.write_account(self.account_data())
        context = self.fake_context()
        profile = profile_dir_for("douyin", self.account_file)
        with patch.object(conf, "LOCAL_CHROME_IMPORT_STORAGE_STATE", False):
            import asyncio

            summary = asyncio.run(
                import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
            )
        self.assertFalse(summary["enabled"])
        context.add_cookies.assert_not_called()

    def test_unknown_platform_imports_all_domains(self):
        self.write_account(self.account_data())
        context = self.fake_context()
        profile = profile_dir_for("unknown", self.account_file)

        import asyncio

        asyncio.run(import_storage_state_if_needed(context, profile, self.account_file))
        cookies = context.add_cookies.await_args.args[0]
        self.assertEqual({cookie["name"] for cookie in cookies}, {"sessionid", "other"})

    def test_batch_failure_retries_individually(self):
        self.write_account(
            {
                "cookies": [
                    {
                        "name": "one",
                        "value": "1",
                        "domain": ".douyin.com",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": "two",
                        "value": "2",
                        "domain": ".douyin.com",
                        "path": "/",
                        "expires": -1,
                    },
                ],
                "origins": [],
            }
        )
        context = self.fake_context()
        context.add_cookies = AsyncMock(
            side_effect=[RuntimeError("batch rejected"), None, RuntimeError("bad")]
        )
        profile = profile_dir_for("douyin", self.account_file)

        import asyncio

        summary = asyncio.run(
            import_storage_state_if_needed(context, profile, self.account_file, platform="douyin")
        )
        self.assertEqual(summary["cookies"], 1)
        self.assertEqual(context.add_cookies.await_count, 3)


class ExportTests(ChromeTestCase):
    def test_export_writes_json_atomically(self):
        context = self.fake_context()

        async def fake_storage_state(path=None):
            Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")

        context.storage_state = AsyncMock(side_effect=fake_storage_state)

        import asyncio

        result = asyncio.run(export_storage_state(context, self.account_file))

        self.assertTrue(result)
        self.assertEqual(
            json.loads(self.account_file.read_text(encoding="utf-8")),
            {"cookies": [], "origins": []},
        )
        leftovers = list(self.account_file.parent.glob("*.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_export_failure_keeps_previous_file(self):
        self.write_account('{"cookies": ["old"], "origins": []}')
        context = self.fake_context()
        context.storage_state = AsyncMock(side_effect=RuntimeError("cdp closed"))

        import asyncio

        result = asyncio.run(export_storage_state(context, self.account_file))

        self.assertFalse(result)
        self.assertIn("old", self.account_file.read_text(encoding="utf-8"))
        self.assertEqual(list(self.account_file.parent.glob("*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
