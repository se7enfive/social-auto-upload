# Language: 中文
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_commands.tencent as tencent_command
import uploader.tencent_uploader.main as tencent_main
from utils.local_chrome import LocalChromeError
from utils.publish_safety import PublishLockError, session_lock


class FakeAsyncPlaywright:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TencentStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")})
        self._env.start()
        self.account_file = self.tmp / "cookies" / "tencent_creator.json"
        self.account_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TencentCommandLockTests(TencentStateTestCase):
    def setUp(self):
        super().setUp()
        self._resolve = patch.object(
            tencent_command, "resolve_account_file", return_value=self.account_file
        )
        self._resolve.start()

    def tearDown(self):
        self._resolve.stop()
        super().tearDown()

    def test_login_check_and_upload_take_account_lock(self):
        request = tencent_command.TencentVideoUploadRequest(
            account_name="creator",
            video_file=Path("demo.mp4"),
            title="标题",
            description="",
            tags=[],
            publish_date=0,
        )
        calls = {
            "login": lambda: tencent_command.login_tencent_account("creator"),
            "check": lambda: tencent_command.check_tencent_account("creator"),
            "upload": lambda: tencent_command.upload_tencent_video(request),
        }
        for name, call in calls.items():
            with self.subTest(entry=name):
                with session_lock(self.account_file, "tencent", "creator"):
                    with self.assertRaises(PublishLockError):
                        asyncio.run(call())


class TencentCookieGenTests(TencentStateTestCase):
    def setUp(self):
        super().setUp()
        self.context = MagicMock()
        self.page = MagicMock()
        self.page.goto = AsyncMock()
        self.page.url = tencent_main.TENCENT_UPLOAD_URL
        self.context.new_page = AsyncMock(return_value=self.page)
        self.context.close = AsyncMock()
        self.launch = AsyncMock(return_value=self.context)
        self.export = AsyncMock(return_value=True)
        self.no_nested_auth = AsyncMock(side_effect=AssertionError("不得嵌套调用 cookie_auth"))
        self.playwright_manager = MagicMock(
            return_value=FakeAsyncPlaywright(MagicMock())
        )

    def test_existing_login_reuses_same_context_without_qrcode(self):
        save_qrcode = AsyncMock(side_effect=AssertionError("已有登录态不应生成二维码"))
        with (
            patch.object(tencent_main, "launch_local_chrome", self.launch),
            patch.object(tencent_main, "export_storage_state", self.export),
            patch.object(tencent_main, "cookie_auth", self.no_nested_auth),
            patch.object(tencent_main, "_tencent_page_logged_in", AsyncMock(return_value=True)),
            patch.object(tencent_main, "_save_tencent_qrcode", save_qrcode),
            patch.object(tencent_main, "async_playwright", self.playwright_manager),
        ):
            result = asyncio.run(tencent_main.tencent_cookie_gen(str(self.account_file)))

        self.assertTrue(result["success"])
        self.assertIn("已有", result["message"])
        self.launch.assert_awaited_once()
        self.export.assert_awaited_once()
        self.no_nested_auth.assert_not_called()
        save_qrcode.assert_not_called()
        self.context.close.assert_awaited_once()

    def test_successful_qr_login_rechecks_in_same_context(self):
        qrcode_info = {"image_path": str(self.tmp / "qr.png"), "image_data_url": "data:image/png;base64,x"}
        login_result = tencent_main._build_login_result(
            True, "success", "视频号扫码登录成功", str(self.account_file), qrcode_info, tencent_main.TENCENT_UPLOAD_URL
        )
        recheck = AsyncMock(return_value=True)
        with (
            patch.object(tencent_main, "launch_local_chrome", self.launch),
            patch.object(tencent_main, "export_storage_state", self.export),
            patch.object(tencent_main, "cookie_auth", self.no_nested_auth),
            patch.object(
                tencent_main, "_tencent_page_logged_in", AsyncMock(return_value=False)
            ),
            patch.object(
                tencent_main, "_save_tencent_qrcode", AsyncMock(return_value=qrcode_info)
            ),
            patch.object(
                tencent_main, "_wait_for_tencent_login", AsyncMock(return_value=login_result)
            ),
            patch.object(tencent_main, "_recheck_tencent_login", recheck),
            patch.object(tencent_main.asyncio, "sleep", AsyncMock()),
            patch.object(tencent_main, "async_playwright", self.playwright_manager),
        ):
            result = asyncio.run(tencent_main.tencent_cookie_gen(str(self.account_file)))

        self.assertTrue(result["success"])
        self.launch.assert_awaited_once()
        self.export.assert_awaited_once()
        recheck.assert_awaited_once()
        self.no_nested_auth.assert_not_called()
        self.context.close.assert_awaited_once()


class TencentUploaderChromeTests(TencentStateTestCase):
    def _video(self):
        return tencent_main.TencentVideo(
            title="标题",
            file_path=str(self.tmp / "demo.mp4"),
            tags=[],
            publish_date=0,
            account_file=str(self.account_file),
        )

    def test_upload_launches_local_chrome_once_and_exports_backup(self):
        context = MagicMock()
        context.new_page = AsyncMock(return_value=MagicMock())
        context.close = AsyncMock()
        launch = AsyncMock(return_value=context)
        export = AsyncMock(return_value=True)
        video = self._video()
        video.validate_upload_args = AsyncMock()
        video.open_upload_page = AsyncMock()
        video.upload_video_file = AsyncMock()
        video.prepare_video_for_publish = AsyncMock()
        video.wait_for_upload_complete = AsyncMock()
        video.apply_collection = AsyncMock()
        video.apply_original_statement = AsyncMock()
        video.set_thumbnail = AsyncMock()
        video.set_short_title = AsyncMock()
        video.submit_publish = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(side_effect=AssertionError("不得启动 Chromium"))

        with (
            patch.object(tencent_main, "launch_local_chrome", launch),
            patch.object(tencent_main, "export_storage_state", export),
        ):
            asyncio.run(video.upload(playwright))

        launch.assert_awaited_once()
        playwright.chromium.launch.assert_not_called()
        export.assert_awaited_once()
        context.close.assert_awaited_once()

    def test_wait_for_upload_complete_uses_draft_button(self):
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 1

            async def get_attribute(self, name):
                return ""

            async def is_disabled(self):
                return False

        class FakePage:
            def __init__(self):
                self.selectors = []

            def locator(self, selector):
                self.selectors.append(selector)
                return FakeLocator()

        uploader = object.__new__(tencent_main.TencentBaseUploader)
        uploader.is_draft = True
        page = FakePage()
        asyncio.run(uploader.wait_for_upload_complete(page, timeout_seconds=5))

        self.assertTrue(page.selectors)
        self.assertIn("保存草稿", page.selectors[0])
        self.assertNotIn("发表", page.selectors[0])

    def test_wait_for_upload_complete_uses_publish_button(self):
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 1

            async def get_attribute(self, name):
                return ""

            async def is_disabled(self):
                return False

        class FakePage:
            def __init__(self):
                self.selectors = []

            def locator(self, selector):
                self.selectors.append(selector)
                return FakeLocator()

        uploader = object.__new__(tencent_main.TencentBaseUploader)
        uploader.is_draft = False
        page = FakePage()
        asyncio.run(uploader.wait_for_upload_complete(page, timeout_seconds=5))

        self.assertTrue(page.selectors)
        self.assertIn("发表", page.selectors[0])
        self.assertNotIn("保存草稿", page.selectors[0])

    def test_save_draft_waits_for_post_draft_response(self):
        class FakeResponse:
            url = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_draft?x=1"
            status = 201

        class FakeResponseInfo:
            def __init__(self, response):
                self._response = response

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            @property
            def value(self):
                async def get():
                    return self._response

                return get()

        class FakePage:
            def __init__(self, response):
                self.response = response
                self.predicates = []

            def expect_response(self, predicate, timeout=None):
                self.predicates.append(predicate)
                return FakeResponseInfo(self.response)

        uploader = object.__new__(tencent_main.TencentBaseUploader)
        button = MagicMock()
        button.click = AsyncMock()
        page = FakePage(FakeResponse())

        asyncio.run(uploader._save_draft(page, button))

        button.click.assert_awaited_once()
        self.assertTrue(page.predicates[0](FakeResponse()))

    def test_save_draft_fails_on_http_error(self):
        class FakeResponse:
            url = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_draft"
            status = 500

        class FakeResponseInfo:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            @property
            def value(self):
                async def get():
                    return FakeResponse()

                return get()

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"

            def expect_response(self, predicate, timeout=None):
                return FakeResponseInfo()

        uploader = object.__new__(tencent_main.TencentBaseUploader)
        button = MagicMock()
        button.click = AsyncMock()
        page = FakePage()

        with patch.object(tencent_main.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "草稿保存"):
                asyncio.run(uploader._save_draft(page, button))
        self.assertEqual(button.click.await_count, 3)

    def test_validate_base_args_is_local_only(self):
        video = self._video()
        with patch.object(tencent_main, "has_local_session", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "sau tencent login"):
                asyncio.run(video.validate_base_args())
        with patch.object(tencent_main, "launch_local_chrome", AsyncMock(side_effect=AssertionError)):
            with patch.object(tencent_main, "has_local_session", return_value=True):
                asyncio.run(video.validate_base_args())

    def test_validate_base_args_rejects_headless(self):
        video = self._video()
        video.headless = True
        with self.assertRaisesRegex(LocalChromeError, "有头"):
            asyncio.run(video.validate_base_args())


if __name__ == "__main__":
    unittest.main()
