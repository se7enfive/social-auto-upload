# Language: 中文
import argparse
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from sau_commands import douyin as douyin_command
from sau_commands.douyin import DouyinVideoUploadRequest
from uploader.douyin_uploader.http_engine import DouyinHttpPreflightError
from utils.publish_safety import STATUS_FAILED_BEFORE_SUBMIT


class CliParserTests(unittest.TestCase):
    def _parser(self):
        with patch.object(douyin_command, "default_upload_engine", return_value="auto"):
            return sau_cli.build_parser()

    def test_upload_video_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "demo.mp4"
            video.write_bytes(b"video")
            args = self._parser().parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "ops",
                    "--file",
                    str(video),
                    "--title",
                    "标题",
                ]
            )
        self.assertEqual(args.engine, "auto")
        self.assertEqual(args.visibility, "public")
        self.assertFalse(args.skip_ai_declaration)

    def test_upload_video_http_flags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "demo.mp4"
            video.write_bytes(b"video")
            args = self._parser().parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "ops",
                    "--file",
                    str(video),
                    "--title",
                    "标题",
                    "--engine",
                    "http",
                    "--skip-ai-declaration",
                    "--visibility",
                    "private",
                ]
            )
        self.assertEqual(args.engine, "http")
        self.assertTrue(args.skip_ai_declaration)
        self.assertEqual(args.visibility, "private")

    def test_login_and_check_engine_flags(self):
        parser = self._parser()
        login_args = parser.parse_args(
            ["douyin", "login", "--account", "ops", "--engine", "http", "--force"]
        )
        self.assertEqual(login_args.engine, "http")
        self.assertTrue(login_args.force)
        check_args = parser.parse_args(
            ["douyin", "check", "--account", "ops", "--engine", "browser"]
        )
        self.assertEqual(check_args.engine, "browser")

    def test_setup_flags(self):
        args = self._parser().parse_args(
            [
                "douyin",
                "setup",
                "--accept-third-party",
                "--source",
                "C:/tmp/DouYin_Spider",
                "--force",
                "--timeout",
                "60",
            ]
        )
        self.assertTrue(args.accept_third_party)
        self.assertEqual(args.source, Path("C:/tmp/DouYin_Spider"))
        self.assertTrue(args.force)
        self.assertEqual(args.timeout, 60)

    def test_upload_note_does_not_accept_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "1.png"
            image.write_bytes(b"image")
            with self.assertRaises(SystemExit):
                self._parser().parse_args(
                    [
                        "douyin",
                        "upload-note",
                        "--account",
                        "ops",
                        "--images",
                        str(image),
                        "--title",
                        "标题",
                        "--engine",
                        "http",
                    ]
                )


class RunDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_passes_engine_and_force(self):
        args = argparse.Namespace(
            action="login",
            account="ops",
            headless=False,
            engine="http",
            force=True,
        )
        login = AsyncMock(
            return_value={
                "success": True,
                "account_file": "cookies/douyin_http_ops.json",
                "reused": True,
            }
        )
        with patch.object(douyin_command, "login_douyin_account", login):
            code = await douyin_command.run(args)
        self.assertEqual(code, 0)
        login.assert_awaited_once_with(
            "ops", headless=False, engine="http", force=True
        )

    async def test_setup_runs_installer_in_thread(self):
        args = argparse.Namespace(
            action="setup",
            accept_third_party=True,
            source=None,
            force=False,
            timeout=60,
        )
        installer = MagicMock(return_value=Path("C:/runtime"))
        with patch.object(douyin_command, "ensure_runtime", installer):
            code = await douyin_command.run(args)
        self.assertEqual(code, 0)
        installer.assert_called_once_with(
            True, source_dir=None, force=False, timeout=60
        )

    async def test_old_namespace_defaults_to_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "demo.mp4"
            video.write_bytes(b"video")
            args = argparse.Namespace(
                action="upload-video",
                account="ops",
                file=video,
                title="标题",
                desc="",
                tags="",
                schedule=None,
                thumbnail=None,
                thumbnail_landscape=None,
                thumbnail_portrait=None,
                product_link="",
                product_title="",
                declaration=None,
                collection=None,
                debug=False,
                headless=False,
                prepare_only=False,
                result=None,
                request_id=None,
                timeout=60,
            )
            upload = AsyncMock()
            with patch.object(douyin_command, "upload_video", upload):
                code = await douyin_command.run(args)
        self.assertEqual(code, 0)
        request = upload.await_args.args[0]
        self.assertEqual(request.engine, "browser")
        self.assertFalse(request.skip_ai_declaration)
        self.assertEqual(request.visibility, "public")


class UploadBranchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "video.mp4"
        self.video.write_bytes(b"video-bytes")
        self.cover = self.tmp / "cover.jpg"
        self.cover.write_bytes(b"cover-bytes")
        self._state = patch.dict(
            os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")}
        )
        self._state.start()

    async def asyncTearDown(self):
        self._state.stop()
        self._tmp.cleanup()

    def _request(self, **kwargs):
        options = {
            "account_name": "ops",
            "video_file": self.video,
            "title": "高层火灾",
            "description": "往上跑还是往下跑",
            "tags": ["消防"],
            "publish_date": 0,
            "thumbnail_file": self.cover,
            "engine": "http",
            "skip_ai_declaration": True,
        }
        options.update(kwargs)
        return DouyinVideoUploadRequest(**options)

    async def test_http_branch_never_starts_browser(self):
        upload_http = AsyncMock()
        setup = AsyncMock(return_value=True)
        with (
            patch.object(
                douyin_command,
                "resolve_account_file",
                return_value=self.tmp / "douyin_ops.json",
            ),
            patch.object(douyin_command, "resolve_upload_engine", return_value="http"),
            patch.object(douyin_command, "upload_video_http", upload_http),
            patch.object(douyin_command, "douyin_setup", setup),
            patch.object(douyin_command, "DouYinVideo") as video_cls,
        ):
            await douyin_command.upload_video(self._request())
        upload_http.assert_awaited_once()
        setup.assert_not_awaited()
        video_cls.assert_not_called()

    async def test_http_preflight_failure_exports_failed_before_submit(self):
        request = self._request(result_file=self.tmp / "result.json")

        async def fail(_request, _attempt):
            raise DouyinHttpPreflightError("没有封面")

        with (
            patch.object(
                douyin_command,
                "resolve_account_file",
                return_value=self.tmp / "douyin_ops.json",
            ),
            patch.object(douyin_command, "resolve_upload_engine", return_value="http"),
            patch.object(douyin_command, "upload_video_http", side_effect=fail),
            patch.object(douyin_command, "douyin_setup", AsyncMock()),
        ):
            with self.assertRaises(DouyinHttpPreflightError):
                await douyin_command.upload_video(request)

        payload = json.loads((self.tmp / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], STATUS_FAILED_BEFORE_SUBMIT)
        self.assertFalse(payload["submit_attempted"])


class FingerprintTests(unittest.TestCase):
    def _request(self, **kwargs):
        options = {
            "account_name": "ops",
            "video_file": Path("video.mp4"),
            "title": "标题",
            "description": "正文",
            "tags": ["tag"],
            "publish_date": 0,
        }
        options.update(kwargs)
        return DouyinVideoUploadRequest(**options)

    def test_engine_and_visibility_change_fingerprint(self):
        base = self._request()
        browser = douyin_command._video_fingerprint(base, engine="browser")
        http = douyin_command._video_fingerprint(base, engine="http")
        private = douyin_command._video_fingerprint(
            self._request(visibility="private"), engine="browser"
        )
        skipped = douyin_command._video_fingerprint(
            self._request(skip_ai_declaration=True), engine="browser"
        )
        self.assertEqual(len({browser, http, private, skipped}), 4)


class AuthEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_auto_prefers_http_only_with_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "douyin_http_ops.json"
            credentials.write_text("{}", encoding="utf-8")
            check_http = AsyncMock(return_value=True)
            with (
                patch.object(douyin_command, "is_runtime_installed", return_value=True),
                patch.object(
                    douyin_command,
                    "resolve_http_account_file",
                    return_value=credentials,
                ),
                patch.object(
                    douyin_command,
                    "resolve_account_file",
                    return_value=Path(temp_dir) / "douyin_ops.json",
                ),
                patch.object(douyin_command, "check_douyin_http", check_http),
            ):
                self.assertTrue(
                    await douyin_command.check_douyin_account("ops", engine="auto")
                )
            check_http.assert_awaited_once()

    async def test_http_login_requires_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(douyin_command, "is_runtime_installed", return_value=True),
                patch.object(
                    douyin_command,
                    "resolve_account_file",
                    return_value=Path(temp_dir) / "douyin_ops.json",
                ),
                patch.object(
                    douyin_command, "has_interactive_terminal", return_value=False
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "交互式终端"):
                    await douyin_command.login_douyin_account(
                        "ops", engine="http"
                    )


if __name__ == "__main__":
    unittest.main()
