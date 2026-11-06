# Language: 中文
import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from sau_commands import douyin as douyin_command
from sau_commands import safety as safety_command
from uploader.douyin_uploader import main as douyin_main
from uploader.xiaohongshu_uploader import main as xhs_main
from utils import publish_safety
from utils.publish_safety import (
    Deadline,
    PublicationAttempt,
    PublishAlreadySubmitted,
    PublishBlockedError,
    PublishJournalError,
    PublishLockError,
    PublishOutcomeUnknown,
    PublishRequestConflict,
    STATUS_NEEDS_HUMAN,
    STATUS_NOT_SUBMITTED,
    STATUS_PREPARED,
    STATUS_FAILED_BEFORE_SUBMIT,
    STATUS_SUBMISSION_UNKNOWN,
    STATUS_SUBMITTED,
    atomic_write_json,
    close_browser_resources,
    find_unresolved,
    load_records,
    resolve_record,
    session_lock,
    submit_once,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class AtomicWriteTests(TempStateTestCase):
    def test_atomic_write_replaces_complete_file(self):
        path = self.tmp / "a.json"
        atomic_write_json(path, {"value": 1})
        atomic_write_json(path, {"value": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 2})

    def test_write_failure_keeps_previous_file_and_cleans_temp(self):
        path = self.tmp / "a.json"
        atomic_write_json(path, {"value": 1})
        with patch.object(publish_safety.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(PublishJournalError):
                atomic_write_json(path, {"value": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1})
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])


class DeadlineTests(unittest.TestCase):
    def test_expired_and_remaining(self):
        deadline = Deadline(0.02)
        time.sleep(0.03)
        self.assertTrue(deadline.expired)
        with self.assertRaises(publish_safety.PublishDeadlineExceeded):
            deadline.raise_if_expired("test")

    def test_clamp_respects_budget_and_floor(self):
        deadline = Deadline(10)
        self.assertEqual(deadline.clamp(5), 5)
        self.assertLessEqual(deadline.clamp(100), 10)
        self.assertEqual(deadline.clamp(0.01), 0.5)

    def test_invalid_timeout_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                publish_safety.validate_timeout(value)

    def test_request_id_rejects_path_like_or_empty_values(self):
        self.assertIsNone(publish_safety.validate_request_id(None))
        self.assertIsNone(publish_safety.validate_request_id("   "))
        for value in ("../evil", "a/b", "a" * 129, "中文"):
            with self.assertRaises(ValueError):
                publish_safety.validate_request_id(value)


class SessionLockTests(TempStateTestCase):
    def test_reentrant_same_process_fails_fast(self):
        account = self.tmp / "cookie.json"
        with session_lock(account, "douyin", "creator"):
            with self.assertRaises(PublishLockError):
                with session_lock(account, "douyin", "creator"):
                    pass

    def test_different_accounts_can_run_in_parallel(self):
        with session_lock(self.tmp / "a.json", "douyin", "a"):
            with session_lock(self.tmp / "b.json", "douyin", "b"):
                pass

    def test_cross_process_conflict_and_release_on_exit(self):
        account = self.tmp / "cookie.json"
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from utils.publish_safety import session_lock\n"
            "with session_lock(Path(sys.argv[1]), 'douyin', 'holder'):\n"
            "    print('READY', flush=True)\n"
            "    sys.stdin.read(1)\n"
        )
        env = dict(os.environ)
        env["SAU_STATE_DIR"] = str(self.tmp / "state")
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(account)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = process.stdout.readline().strip()
            if ready != "READY":
                process.stdin.close()
                process.wait(timeout=10)
                self.fail(f"holder process failed: {process.stderr.read()}")
            with self.assertRaises(PublishLockError):
                with session_lock(account, "douyin", "creator"):
                    pass
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=10)

        # 子进程退出后 OS 自动释放锁，不需要删除锁文件。
        with session_lock(account, "douyin", "creator"):
            pass


class PublicationAttemptTests(TempStateTestCase):
    def _attempt(self, **kwargs):
        return PublicationAttempt(
            platform="douyin",
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint=kwargs.pop("fingerprint", "fp-1"),
            timeout=60,
            **kwargs,
        )

    def test_prepare_submit_is_conservatively_unknown_and_blocks(self):
        attempt = self._attempt(request_id="req-1")
        attempt.ensure_can_start()
        attempt.begin()
        attempt.prepare_submit({"stage": "submitting"})

        record = load_records("douyin", "creator")[0]
        self.assertEqual(record["status"], STATUS_SUBMISSION_UNKNOWN)
        self.assertTrue(record["submit_attempted"])

        with self.assertRaises(PublishBlockedError):
            self._attempt(request_id="req-2").ensure_can_start()

    def test_resolve_not_submitted_allows_retry(self):
        attempt = self._attempt()
        attempt.begin()
        attempt.prepare_submit()

        resolved = resolve_record(
            "douyin", "creator", attempt.run_id, "not-submitted", "checked platform"
        )
        self.assertEqual(resolved["status"], STATUS_NOT_SUBMITTED)
        self._attempt().ensure_can_start()

    def test_same_request_id_same_content_is_not_republished(self):
        attempt = self._attempt(request_id="req-1")
        attempt.begin()
        attempt.prepare_submit()
        attempt.mark_submitted()

        with self.assertRaises(PublishAlreadySubmitted):
            self._attempt(request_id="req-1").ensure_can_start()

    def test_same_request_id_different_content_is_rejected(self):
        attempt = self._attempt(request_id="req-1")
        attempt.begin()
        attempt.prepare_submit()
        attempt.mark_submitted()

        with self.assertRaises(PublishRequestConflict):
            self._attempt(request_id="req-1", fingerprint="fp-2").ensure_can_start()

    def test_failed_before_submit_allows_same_request_retry(self):
        attempt = self._attempt(request_id="req-1")
        attempt.begin()
        attempt.mark_failed_before_submit("cookie invalid")
        self._attempt(request_id="req-1").ensure_can_start()

    def test_post_submit_failure_never_downgrades_to_retryable(self):
        attempt = self._attempt()
        attempt.begin()
        attempt.prepare_submit()
        attempt.mark_failed_before_submit("late failure")
        attempt.finalize_on_error("late failure")
        self.assertEqual(attempt.status, STATUS_SUBMISSION_UNKNOWN)
        self.assertTrue(attempt.submit_attempted)

    def test_needs_human_blocks_until_resolved(self):
        attempt = self._attempt()
        attempt.begin()
        attempt.mark_needs_human("sms timeout")

        with self.assertRaises(PublishBlockedError):
            self._attempt().ensure_can_start()

        resolve_record("douyin", "creator", attempt.run_id, "not-submitted", "sms done")
        self._attempt().ensure_can_start()

    def test_prepare_only_does_not_block(self):
        attempt = self._attempt(prepare_only=True)
        attempt.begin()
        attempt.mark_prepared({"title": "t"})

        self.assertEqual(load_records("douyin", "creator")[0]["status"], STATUS_PREPARED)
        self._attempt().ensure_can_start()

    def test_prepared_status_is_not_downgraded_by_later_error(self):
        attempt = self._attempt(prepare_only=True)
        attempt.begin()
        attempt.mark_prepared({"title": "t"})
        attempt.finalize_on_error("cookie write failed")
        self.assertEqual(attempt.status, STATUS_PREPARED)

    def test_prepare_submit_write_failure_keeps_retryable_state(self):
        attempt = self._attempt()
        attempt.begin()
        with patch.object(
            publish_safety, "atomic_write_json", side_effect=PublishJournalError("write failed")
        ):
            with self.assertRaises(PublishJournalError):
                attempt.prepare_submit()
        self.assertEqual(attempt.status, "preparing")
        self.assertFalse(attempt.submit_attempted)

    def test_corrupt_record_is_ignored_and_reported(self):
        runs = publish_safety.account_journal_dir("douyin", "creator") / "runs"
        runs.mkdir(parents=True)
        (runs / ("a" * 32 + ".json")).write_text("{not json", encoding="utf-8")

        self.assertEqual(find_unresolved("douyin", "creator"), [])
        self.assertEqual(len(publish_safety.corrupt_record_paths("douyin", "creator")), 1)


class SubmitOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _attempt(self, **kwargs):
        attempt = PublicationAttempt(
            platform="douyin",
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint="fp",
            timeout=60,
            **kwargs,
        )
        attempt.begin()
        return attempt

    async def test_click_and_wait_success_marks_submitted(self):
        attempt = self._attempt()
        clicks = []

        async def click():
            clicks.append(1)

        async def wait_success(timeout):
            return None

        await submit_once(safety=attempt, click=click, wait_success=wait_success)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(attempt.status, STATUS_SUBMITTED)

    async def test_wait_failure_marks_unknown_and_calls_risk_probe(self):
        attempt = self._attempt()
        risks = []

        async def click():
            return None

        async def wait_success(timeout):
            raise TimeoutError("no redirect")

        async def detect_risk():
            risks.append(1)
            return "captcha"

        with self.assertRaises(PublishOutcomeUnknown):
            await submit_once(
                safety=attempt,
                click=click,
                wait_success=wait_success,
                detect_risk=detect_risk,
            )
        self.assertEqual(attempt.status, STATUS_SUBMISSION_UNKNOWN)
        self.assertIn("captcha", attempt.reason)
        self.assertEqual(risks, [1])

    async def test_click_failure_marks_unknown(self):
        attempt = self._attempt()

        async def click():
            raise RuntimeError("click failed")

        async def wait_success(timeout):
            raise AssertionError("must not wait after a failed click")

        with self.assertRaises(PublishOutcomeUnknown):
            await submit_once(safety=attempt, click=click, wait_success=wait_success)
        self.assertEqual(attempt.status, STATUS_SUBMISSION_UNKNOWN)

    async def test_journal_failure_before_click_prevents_click(self):
        attempt = self._attempt()
        clicks = []

        async def click():
            clicks.append(1)

        async def wait_success(timeout):
            return None

        with patch.object(
            publish_safety, "atomic_write_json", side_effect=PublishJournalError("write failed")
        ):
            with self.assertRaises(PublishJournalError):
                await submit_once(safety=attempt, click=click, wait_success=wait_success)
        self.assertEqual(clicks, [])

    async def test_cancellation_after_click_marks_unknown(self):
        attempt = self._attempt()

        async def click():
            return None

        async def wait_success(timeout):
            await asyncio.sleep(10)

        task = asyncio.create_task(
            submit_once(safety=attempt, click=click, wait_success=wait_success)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(attempt.status, STATUS_SUBMISSION_UNKNOWN)


class CloseResourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_close_failure_still_closes_browser(self):
        context = MagicMock()
        context.close = AsyncMock(side_effect=OSError("close failed"))
        browser = MagicMock()
        browser.close = AsyncMock()

        errors = await close_browser_resources(context, browser)

        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        self.assertTrue(errors)


class RiskProbeTests(unittest.IsolatedAsyncioTestCase):
    def _page(self, url, visible):
        locator = MagicMock()
        locator.first = locator
        locator.count = AsyncMock(return_value=1)
        locator.is_visible = AsyncMock(return_value=visible)
        page = MagicMock()
        page.url = url
        page.locator = MagicMock(return_value=locator)
        return page

    async def test_douyin_hidden_verify_component_is_not_risk(self):
        page = self._page(
            "https://creator.douyin.com/creator-micro/content/publish", visible=False
        )
        self.assertIsNone(await douyin_main._douyin_risk_reason(page))

    async def test_douyin_visible_captcha_component_is_risk(self):
        page = self._page(
            "https://creator.douyin.com/creator-micro/content/publish", visible=True
        )
        self.assertIn("验证组件", await douyin_main._douyin_risk_reason(page))

    async def test_xhs_risk_url_is_detected(self):
        page = self._page("https://creator.xiaohongshu.com/captcha?redirect=x", visible=False)
        reason = await xhs_main._xhs_risk_reason(page)
        self.assertIn("风险验证页面", reason)


class SafetyCommandTests(TempStateTestCase):
    def _blocking_attempt(self):
        attempt = PublicationAttempt(
            platform="douyin",
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint="fp",
            timeout=60,
        )
        attempt.begin()
        attempt.prepare_submit()
        return attempt

    def test_status_reports_unresolved(self):
        attempt = self._blocking_attempt()
        args = MagicMock(
            platform="safety", action="status", target_platform="douyin", account="creator"
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = asyncio.run(sau_cli.dispatch(args))

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["unresolved"]), 1)
        self.assertEqual(payload["unresolved"][0]["run_id"], attempt.run_id)

    def test_resolve_via_dispatch_unblocks(self):
        attempt = self._blocking_attempt()
        args = MagicMock(
            platform="safety",
            action="resolve",
            target_platform="douyin",
            account="creator",
            run_id=attempt.run_id,
            outcome="not-submitted",
            reason="checked manually",
        )
        buffer = io.StringIO()
        with (
            patch.object(
                safety_command, "resolve_account_file", return_value=self.tmp / "cookie.json"
            ),
            redirect_stdout(buffer),
        ):
            code = asyncio.run(sau_cli.dispatch(args))

        self.assertEqual(code, 0)
        self.assertEqual(find_unresolved("douyin", "creator"), [])


def _fake_douyin_browser():
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()
    page.eval_on_selector = AsyncMock(return_value="semi-switch-checked")
    page.screenshot = AsyncMock()
    page.url = "https://creator.douyin.com/creator-micro/content/upload"

    generic = MagicMock()
    generic.first = generic
    generic.count = AsyncMock(return_value=1)
    generic.wait_for = AsyncMock()
    generic.set_input_files = AsyncMock()
    generic.is_visible = AsyncMock(return_value=False)
    generic.click = AsyncMock()
    page.locator = MagicMock(return_value=generic)

    text_locator = MagicMock()
    text_locator.first = text_locator
    text_locator.is_visible = AsyncMock(return_value=False)
    text_locator.click = AsyncMock()
    page.get_by_text = MagicMock(return_value=text_locator)

    button = MagicMock()
    button.first = button
    button.wait_for = AsyncMock()
    button.click = AsyncMock()
    page.get_by_role = MagicMock(return_value=button)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    context.storage_state = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    return playwright, page, button


def _fake_xhs_browser():
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.url = "https://creator.xiaohongshu.com/publish/publish"

    preview = MagicMock()
    preview.inner_text = AsyncMock(return_value="上传成功")
    upload_input = MagicMock()
    upload_input.query_selector = AsyncMock(return_value=preview)

    generic = MagicMock()
    generic.first = generic
    generic.count = AsyncMock(return_value=1)
    generic.wait_for = AsyncMock()
    generic.set_input_files = AsyncMock()
    generic.is_visible = AsyncMock(return_value=False)
    generic.click = AsyncMock()

    button = MagicMock()
    button.first = button
    button.wait_for = AsyncMock()
    button.click = AsyncMock()

    def locator(selector):
        if "button:has-text" in selector:
            return button
        return generic

    page.locator = MagicMock(side_effect=locator)
    page.wait_for_selector = AsyncMock(return_value=upload_input)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    context.storage_state = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    return playwright, page, button


class UploaderSafetyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _attempt(self, platform="douyin", prepare_only=False):
        attempt = PublicationAttempt(
            platform=platform,
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint="fp",
            prepare_only=prepare_only,
            timeout=60,
        )
        attempt.begin()
        return attempt

    async def _run_douyin_video(self, prepare_only):
        playwright, page, button = _fake_douyin_browser()
        attempt = self._attempt(prepare_only=prepare_only)
        video = douyin_main.DouYinVideo(
            "标题",
            str(self.tmp / "demo.mp4"),
            [],
            0,
            str(self.tmp / "cookie.json"),
            safety=attempt,
        )
        video.validate_upload_args = AsyncMock()
        video.fill_title_and_description = AsyncMock()
        video.apply_self_declaration = AsyncMock()
        video.apply_collection = AsyncMock()
        video.set_thumbnail = AsyncMock()
        video.handle_auto_video_cover = AsyncMock(return_value=False)
        with (
            patch.object(douyin_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(douyin_main, "_douyin_risk_reason", AsyncMock(return_value=None)),
            patch.object(douyin_main.asyncio, "sleep", AsyncMock()),
        ):
            await video.upload(playwright)
        return attempt, button

    async def test_douyin_video_prepare_only_never_clicks_publish(self):
        attempt, button = await self._run_douyin_video(prepare_only=True)
        self.assertEqual(button.click.await_count, 0)
        self.assertEqual(attempt.status, STATUS_PREPARED)

    async def test_douyin_video_clicks_publish_exactly_once(self):
        attempt, button = await self._run_douyin_video(prepare_only=False)
        self.assertEqual(button.click.await_count, 1)
        self.assertEqual(attempt.status, STATUS_SUBMITTED)

    async def _run_douyin_note(self, prepare_only):
        playwright, page, button = _fake_douyin_browser()
        attempt = self._attempt(prepare_only=prepare_only)
        note = douyin_main.DouYinNote(
            image_paths=[str(self.tmp / "1.png")],
            title="标题",
            note="正文",
            tags=[],
            publish_date=0,
            account_file=str(self.tmp / "cookie.json"),
            safety=attempt,
        )
        note.validate_upload_args = AsyncMock()
        note.fill_title_and_description = AsyncMock()
        with (
            patch.object(douyin_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(douyin_main, "_douyin_risk_reason", AsyncMock(return_value=None)),
            patch.object(douyin_main.asyncio, "sleep", AsyncMock()),
        ):
            await note.upload(playwright)
        return attempt, button

    async def test_douyin_note_prepare_only_never_clicks_publish(self):
        attempt, button = await self._run_douyin_note(prepare_only=True)
        self.assertEqual(button.click.await_count, 0)
        self.assertEqual(attempt.status, STATUS_PREPARED)

    async def test_douyin_note_clicks_publish_exactly_once(self):
        attempt, button = await self._run_douyin_note(prepare_only=False)
        self.assertEqual(button.click.await_count, 1)
        self.assertEqual(attempt.status, STATUS_SUBMITTED)

    async def _run_xhs_video(self, prepare_only):
        playwright, page, button = _fake_xhs_browser()
        attempt = self._attempt(platform="xiaohongshu", prepare_only=prepare_only)
        video = xhs_main.XiaoHongShuVideo(
            title="标题",
            file_path=str(self.tmp / "demo.mp4"),
            tags=[],
            publish_date=0,
            account_file=str(self.tmp / "cookie.json"),
            safety=attempt,
        )
        video.validate_upload_args = AsyncMock()
        video.fill_meta = AsyncMock()
        video.set_thumbnail = AsyncMock()
        video.check_original_declaration = AsyncMock()
        with (
            patch.object(xhs_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(xhs_main, "_xhs_risk_reason", AsyncMock(return_value=None)),
            patch.object(xhs_main.asyncio, "sleep", AsyncMock()),
        ):
            await video.upload(playwright)
        return attempt, button

    async def test_xhs_video_prepare_only_never_clicks_publish(self):
        attempt, button = await self._run_xhs_video(prepare_only=True)
        self.assertEqual(button.click.await_count, 0)
        self.assertEqual(attempt.status, STATUS_PREPARED)

    async def test_xhs_video_clicks_publish_exactly_once(self):
        attempt, button = await self._run_xhs_video(prepare_only=False)
        self.assertEqual(button.click.await_count, 1)
        self.assertEqual(attempt.status, STATUS_SUBMITTED)

    async def _run_xhs_note(self, prepare_only):
        playwright, page, button = _fake_xhs_browser()
        attempt = self._attempt(platform="xiaohongshu", prepare_only=prepare_only)
        note = xhs_main.XiaoHongShuNote(
            image_paths=[str(self.tmp / "1.png")],
            title="标题",
            note="正文",
            tags=[],
            publish_date=0,
            account_file=str(self.tmp / "cookie.json"),
            safety=attempt,
        )
        note.validate_upload_args = AsyncMock()
        note.fill_meta = AsyncMock()
        note.check_original_declaration = AsyncMock()
        with (
            patch.object(xhs_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(xhs_main, "_xhs_risk_reason", AsyncMock(return_value=None)),
            patch.object(xhs_main.asyncio, "sleep", AsyncMock()),
        ):
            await note.upload(playwright)
        return attempt, button

    async def test_xhs_note_prepare_only_never_clicks_publish(self):
        attempt, button = await self._run_xhs_note(prepare_only=True)
        self.assertEqual(button.click.await_count, 0)
        self.assertEqual(attempt.status, STATUS_PREPARED)

    async def test_xhs_note_clicks_publish_exactly_once(self):
        attempt, button = await self._run_xhs_note(prepare_only=False)
        self.assertEqual(button.click.await_count, 1)
        self.assertEqual(attempt.status, STATUS_SUBMITTED)

    async def test_submit_timeout_stays_unknown_when_screenshot_fails(self):
        playwright, page, button = _fake_douyin_browser()

        async def wait_for_url(url, timeout=None, **kwargs):
            if "content/manage" in str(url):
                raise TimeoutError("no redirect")
            return None

        page.wait_for_url = AsyncMock(side_effect=wait_for_url)
        page.screenshot = AsyncMock(side_effect=OSError("screenshot failed"))
        attempt = self._attempt()
        video = douyin_main.DouYinVideo(
            "标题",
            str(self.tmp / "demo.mp4"),
            [],
            0,
            str(self.tmp / "cookie.json"),
            safety=attempt,
        )
        video.validate_upload_args = AsyncMock()
        video.fill_title_and_description = AsyncMock()
        video.apply_self_declaration = AsyncMock()
        video.apply_collection = AsyncMock()
        video.set_thumbnail = AsyncMock()
        video.handle_auto_video_cover = AsyncMock(return_value=False)
        with (
            patch.object(douyin_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)),
            patch.object(douyin_main, "_douyin_risk_reason", AsyncMock(return_value=None)),
            patch.object(douyin_main.asyncio, "sleep", AsyncMock()),
            self.assertRaises(PublishOutcomeUnknown),
        ):
            await video.upload(playwright)

        self.assertEqual(button.click.await_count, 1)
        self.assertEqual(attempt.status, STATUS_SUBMISSION_UNKNOWN)
        page.screenshot.assert_awaited_once()


class CliWrapperSafetyTests(TempStateTestCase):
    def _video_request(self, **kwargs):
        video = self.tmp / "demo.mp4"
        if not video.exists():
            video.write_bytes(b"video")
        options = {
            "account_name": "creator",
            "video_file": video,
            "title": "标题",
            "description": "",
            "tags": [],
            "publish_date": 0,
            "timeout": 60,
        }
        options.update(kwargs)
        return douyin_command.DouyinVideoUploadRequest(**options)

    def test_blocked_account_never_calls_setup(self):
        blocking = PublicationAttempt(
            platform="douyin",
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint="fp",
            timeout=60,
        )
        blocking.begin()
        blocking.prepare_submit()

        setup = AsyncMock(return_value=True)
        with (
            patch.object(
                douyin_command, "resolve_account_file", return_value=self.tmp / "cookie.json"
            ),
            patch.object(douyin_command, "douyin_setup", setup),
            self.assertRaises(PublishBlockedError),
        ):
            asyncio.run(douyin_command.upload_video(self._video_request()))
        setup.assert_not_awaited()

    def test_result_file_is_exported_when_upload_fails_before_submit(self):
        request = self._video_request(result_file=self.tmp / "result.json")

        class FakeUploader:
            def __init__(self, *args, **kwargs):
                pass

            async def douyin_upload_video(self):
                raise RuntimeError("upload failed")

        with (
            patch.object(
                douyin_command, "resolve_account_file", return_value=self.tmp / "cookie.json"
            ),
            patch.object(douyin_command, "douyin_setup", AsyncMock(return_value=True)),
            patch.object(douyin_command, "DouYinVideo", FakeUploader),
            self.assertRaisesRegex(RuntimeError, "upload failed"),
        ):
            asyncio.run(douyin_command.upload_video(request))

        payload = json.loads((self.tmp / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], STATUS_FAILED_BEFORE_SUBMIT)
        self.assertFalse(payload["submit_attempted"])

    def test_run_treats_same_request_id_as_successful_skip(self):
        video = self.tmp / "demo.mp4"
        video.write_bytes(b"video")
        request = douyin_command.DouyinVideoUploadRequest(
            account_name="creator",
            video_file=video,
            title="标题",
            description="",
            tags=[],
            publish_date=0,
            request_id="req-1",
            timeout=60,
        )
        attempt = PublicationAttempt(
            platform="douyin",
            account_name="creator",
            account_file=self.tmp / "cookie.json",
            fingerprint=douyin_command._video_fingerprint(request),
            request_id="req-1",
            timeout=60,
        )
        attempt.begin()
        attempt.prepare_submit()
        attempt.mark_submitted()

        args = argparse.Namespace(
            action="upload-video",
            account="creator",
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
            headless=True,
            prepare_only=False,
            result=None,
            request_id="req-1",
            timeout=60,
        )
        buffer = io.StringIO()
        with (
            patch.object(
                douyin_command, "resolve_account_file", return_value=self.tmp / "cookie.json"
            ),
            redirect_stdout(buffer),
        ):
            code = asyncio.run(douyin_command.run(args))

        self.assertEqual(code, 0)
        self.assertIn("already submitted", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
