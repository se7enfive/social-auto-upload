# Language: 中文
import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from sau_commands import douyin as douyin_command
from sau_commands import xiaohongshu as xiaohongshu_command
from utils import publish_pacing, publish_safety
from utils.publish_pacing import (
    PacingRules,
    PublishPacingConfigError,
    PublishPacingError,
    enforce_publish_pacing,
    evaluate_pacing,
    load_pacing_rules,
    pacing_snapshot,
)
from utils.publish_safety import (
    PublicationAttempt,
    PublishBlockedError,
    STATUS_NEEDS_HUMAN,
    STATUS_PREPARED,
    load_records,
    resolve_record,
)

BASE = datetime(2026, 11, 6, 8, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)


def _record(at, *, attempted=True, status="submitted", **extra):
    stamp = at.isoformat()
    record = {
        "status": status,
        "submit_attempted": attempted,
        "created_at": stamp,
        "updated_at": stamp,
        "evidence": {},
    }
    if attempted:
        record["submit_attempted_at"] = stamp
    record.update(extra)
    return record


class EvaluatePacingTests(unittest.TestCase):
    def test_disabled_rules_always_allow(self):
        decision = evaluate_pacing(PacingRules(), [_record(BASE)], now=BASE)
        self.assertTrue(decision.allowed)

    def test_min_interval_blocks_until_gap_has_passed(self):
        rules = PacingRules(min_interval_seconds=3600)
        records = [_record(BASE)]
        blocked = evaluate_pacing(rules, records, now=BASE + timedelta(minutes=30))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.next_allowed_at, BASE + HOUR)
        self.assertTrue(evaluate_pacing(rules, records, now=BASE + HOUR).allowed)

    def test_legacy_record_without_submit_timestamp_uses_updated_at(self):
        record = _record(BASE)
        del record["submit_attempted_at"]
        rules = PacingRules(min_interval_seconds=3600)
        self.assertFalse(evaluate_pacing(rules, [record], now=BASE + timedelta(minutes=10)).allowed)

    def test_daily_cap_uses_rolling_window_and_reports_first_free_slot(self):
        rules = PacingRules(max_submits_per_24h=2)
        records = [_record(BASE), _record(BASE + HOUR)]
        blocked = evaluate_pacing(rules, records, now=BASE + 2 * HOUR)
        self.assertFalse(blocked.allowed)
        # 只有最早的那次滑出 24 小时窗口，才会空出一个名额
        self.assertEqual(blocked.next_allowed_at, BASE + DAY)
        self.assertFalse(evaluate_pacing(rules, records, now=BASE + DAY - timedelta(seconds=1)).allowed)
        self.assertTrue(evaluate_pacing(rules, records, now=BASE + DAY).allowed)

    def test_prepare_and_pre_click_failures_do_not_count(self):
        rules = PacingRules(min_interval_seconds=3600, max_submits_per_24h=1)
        records = [
            _record(BASE, attempted=False, status=STATUS_PREPARED),
            _record(BASE, attempted=False, status="failed_before_submit"),
        ]
        self.assertTrue(evaluate_pacing(rules, records, now=BASE).allowed)

    def test_needs_human_starts_cooldown(self):
        rules = PacingRules(cooldown_after_risk_seconds=7200)
        records = [_record(BASE, attempted=False, status=STATUS_NEEDS_HUMAN)]
        self.assertFalse(evaluate_pacing(rules, records, now=BASE + HOUR).allowed)
        self.assertTrue(evaluate_pacing(rules, records, now=BASE + 2 * HOUR).allowed)

    def test_resolved_needs_human_still_starts_cooldown_from_resolution(self):
        rules = PacingRules(cooldown_after_risk_seconds=7200)
        resolved_at = BASE + HOUR
        records = [
            _record(
                resolved_at,
                attempted=False,
                status="not_submitted",
                status_before_resolution=STATUS_NEEDS_HUMAN,
            )
        ]
        blocked = evaluate_pacing(rules, records, now=resolved_at + timedelta(minutes=30))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.next_allowed_at, resolved_at + 2 * HOUR)

    def test_post_click_risk_signal_starts_cooldown(self):
        rules = PacingRules(cooldown_after_risk_seconds=7200)
        records = [_record(BASE, status="submission_unknown", evidence={"risk_signal": "captcha"})]
        self.assertFalse(evaluate_pacing(rules, records, now=BASE + HOUR).allowed)

    def test_unreadable_state_fails_closed(self):
        decision = evaluate_pacing(PacingRules(max_submits_per_24h=5), [], now=BASE, unreadable_count=1)
        self.assertFalse(decision.allowed)
        self.assertIsNone(decision.next_allowed_at)

    def test_submit_without_any_timestamp_fails_closed(self):
        record = {"status": "submitted", "submit_attempted": True}
        self.assertFalse(evaluate_pacing(PacingRules(min_interval_seconds=60), [record], now=BASE).allowed)


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"SAU_STATE_DIR": str(self.tmp / "state")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _write_config(self, text: str) -> None:
        path = publish_pacing.pacing_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _new_attempt(self, platform="douyin", **overrides):
        options = {
            "platform": platform,
            "account_name": "creator",
            "account_file": self.tmp / "cookie.json",
            "fingerprint": "fp-next",
            "timeout": 60,
        }
        options.update(overrides)
        return PublicationAttempt(**options)

    def _submit_at(self, at, platform="douyin"):
        with patch.object(publish_safety, "_now", return_value=at.isoformat()):
            attempt = self._new_attempt(platform=platform, fingerprint=f"fp-{at.isoformat()}")
            attempt.begin()
            attempt.prepare_submit()
            attempt.mark_submitted()
        return attempt


class PacingConfigTests(TempStateTestCase):
    def test_missing_config_means_no_limits(self):
        self.assertFalse(load_pacing_rules("douyin").enabled)

    def test_rules_are_loaded_per_platform(self):
        self._write_config(
            json.dumps(
                {
                    "douyin": {
                        "min_interval_seconds": 3600,
                        "max_submits_per_24h": 3,
                        "cooldown_after_risk_seconds": 86400,
                    }
                }
            )
        )
        self.assertEqual(load_pacing_rules("douyin"), PacingRules(3600, 3, 86400))
        self.assertFalse(load_pacing_rules("xiaohongshu").enabled)

    def test_invalid_config_fails_closed(self):
        cases = [
            "{not json",
            json.dumps(["douyin"]),
            json.dumps({"tiktok": {"min_interval_seconds": 60}}),
            json.dumps({"douyin": {"min_interval_sec": 60}}),
            json.dumps({"douyin": {"max_submits_per_24h": True}}),
            json.dumps({"douyin": {"min_interval_seconds": -1}}),
            json.dumps({"douyin": {"min_interval_seconds": 3600000}}),  # 疑似把毫秒当成秒
            json.dumps({"douyin": {"max_submits_per_24h": 1.5}}),
        ]
        for text in cases:
            with self.subTest(config=text):
                self._write_config(text)
                with self.assertRaises(PublishPacingConfigError):
                    load_pacing_rules("douyin")


class JournalPacingTests(TempStateTestCase):
    def test_submit_attempt_time_is_persisted_at_click_boundary(self):
        self._submit_at(BASE)
        record = load_records("douyin", "creator")[0]
        self.assertEqual(record["submit_attempted_at"], BASE.isoformat())

    def test_interval_blocks_second_publish(self):
        self._write_config(json.dumps({"douyin": {"min_interval_seconds": 3600}}))
        self._submit_at(BASE)
        with patch.object(publish_pacing, "_utc_now", return_value=BASE + timedelta(minutes=10)):
            with self.assertRaises(PublishPacingError) as ctx:
                enforce_publish_pacing(self._new_attempt())
        self.assertIsInstance(ctx.exception, PublishBlockedError)
        self.assertIn("再次正式发布", str(ctx.exception))
        self.assertIn("--prepare-only", str(ctx.exception))

    def test_daily_cap_blocks_then_allows_after_window(self):
        self._write_config(json.dumps({"douyin": {"max_submits_per_24h": 1}}))
        self._submit_at(BASE)
        with patch.object(publish_pacing, "_utc_now", return_value=BASE + HOUR):
            with self.assertRaises(PublishPacingError):
                enforce_publish_pacing(self._new_attempt())
        with patch.object(publish_pacing, "_utc_now", return_value=BASE + DAY):
            enforce_publish_pacing(self._new_attempt())

    def test_prepare_only_is_never_limited(self):
        self._write_config(json.dumps({"douyin": {"max_submits_per_24h": 1}}))
        self._submit_at(BASE)
        with patch.object(publish_pacing, "_utc_now", return_value=BASE + HOUR):
            enforce_publish_pacing(self._new_attempt(prepare_only=True))

    def test_other_platform_submissions_do_not_count_against_this_platform(self):
        self._write_config(json.dumps({"douyin": {"max_submits_per_24h": 1}}))
        self._submit_at(BASE, platform="xiaohongshu")
        with patch.object(publish_pacing, "_utc_now", return_value=BASE + HOUR):
            enforce_publish_pacing(self._new_attempt(platform="douyin"))

    def test_cooldown_starts_after_human_resolution(self):
        self._write_config(json.dumps({"douyin": {"cooldown_after_risk_seconds": 7200}}))
        with patch.object(publish_safety, "_now", return_value=BASE.isoformat()):
            attempt = self._new_attempt()
            attempt.begin()
            attempt.mark_needs_human("点击前出现短信验证")
        resolved_at = BASE + HOUR
        with patch.object(publish_safety, "_now", return_value=resolved_at.isoformat()):
            resolve_record("douyin", "creator", attempt.run_id, "not-submitted", "人工确认未发表")
        with patch.object(publish_pacing, "_utc_now", return_value=resolved_at + timedelta(minutes=30)):
            with self.assertRaises(PublishPacingError):
                enforce_publish_pacing(self._new_attempt())
        with patch.object(publish_pacing, "_utc_now", return_value=resolved_at + 2 * HOUR):
            enforce_publish_pacing(self._new_attempt())

    def test_snapshot_reports_next_allowed_time(self):
        self._write_config(json.dumps({"douyin": {"min_interval_seconds": 3600}}))
        self._submit_at(BASE)
        snapshot = pacing_snapshot("douyin", "creator", now=BASE + timedelta(minutes=5))
        self.assertTrue(snapshot["enabled"])
        self.assertFalse(snapshot["allowed_now"])
        self.assertEqual(datetime.fromisoformat(snapshot["next_allowed_at"]), BASE + HOUR)

    def test_snapshot_reports_invalid_config_without_raising(self):
        self._write_config("{bad json")
        self.assertIn("error", pacing_snapshot("douyin", "creator"))


class CliPacingTests(TempStateTestCase):
    def _video_file(self):
        video = self.tmp / "demo.mp4"
        video.write_bytes(b"video")
        return video

    def _douyin_request(self):
        return douyin_command.DouyinVideoUploadRequest(
            account_name="creator",
            video_file=self._video_file(),
            title="标题",
            description="",
            tags=[],
            publish_date=0,
            timeout=60,
        )

    def test_douyin_cli_is_blocked_before_setup(self):
        self._write_config(json.dumps({"douyin": {"max_submits_per_24h": 1}}))
        self._submit_at(datetime.now(timezone.utc))
        setup = AsyncMock(return_value=True)
        with (
            patch.object(douyin_command, "resolve_account_file", return_value=self.tmp / "cookie.json"),
            patch.object(douyin_command, "douyin_setup", setup),
            self.assertRaises(PublishPacingError),
        ):
            asyncio.run(douyin_command.upload_video(self._douyin_request()))
        setup.assert_not_awaited()
        # 节奏阻断发生在建立新 run 记录之前，journal 中不应多出一条记录。
        self.assertEqual(len(load_records("douyin", "creator")), 1)

    def test_xiaohongshu_cli_is_blocked_before_setup(self):
        self._write_config(json.dumps({"xiaohongshu": {"max_submits_per_24h": 1}}))
        self._submit_at(datetime.now(timezone.utc), platform="xiaohongshu")
        request = xiaohongshu_command.XiaohongshuVideoUploadRequest(
            account_name="creator",
            video_file=self._video_file(),
            title="标题",
            description="",
            tags=[],
            publish_date=0,
            timeout=60,
        )
        setup = AsyncMock(return_value=True)
        with (
            patch.object(xiaohongshu_command, "resolve_account_file", return_value=self.tmp / "cookie.json"),
            patch.object(xiaohongshu_command, "xiaohongshu_setup", setup),
            self.assertRaises(PublishPacingError),
        ):
            asyncio.run(xiaohongshu_command.upload_xiaohongshu_video(request))
        setup.assert_not_awaited()
        self.assertEqual(len(load_records("xiaohongshu", "creator")), 1)

    def test_without_config_the_publish_path_is_not_limited(self):
        self._submit_at(datetime.now(timezone.utc))

        class FakeUploader:
            def __init__(self, *args, **kwargs):
                pass

            async def douyin_upload_video(self):
                raise RuntimeError("upload failed")

        setup = AsyncMock(return_value=True)
        with (
            patch.object(douyin_command, "resolve_account_file", return_value=self.tmp / "cookie.json"),
            patch.object(douyin_command, "douyin_setup", setup),
            patch.object(douyin_command, "DouYinVideo", FakeUploader),
            self.assertRaisesRegex(RuntimeError, "upload failed"),
        ):
            asyncio.run(douyin_command.upload_video(self._douyin_request()))
        setup.assert_awaited_once()

    def test_safety_status_reports_pacing(self):
        self._write_config(json.dumps({"douyin": {"max_submits_per_24h": 1}}))
        self._submit_at(datetime.now(timezone.utc))
        args = MagicMock(platform="safety", action="status", target_platform="douyin", account="creator")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = asyncio.run(sau_cli.dispatch(args))

        self.assertEqual(code, 0)
        pacing = json.loads(buffer.getvalue())["pacing"]
        self.assertTrue(pacing["enabled"])
        self.assertFalse(pacing["allowed_now"])


if __name__ == "__main__":
    unittest.main()
