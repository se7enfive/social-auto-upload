# Language: 中文
import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sau_commands.douyin import DouyinVideoUploadRequest
from uploader.douyin_uploader import http_engine
from uploader.douyin_uploader.http_engine import (
    DouyinHttpCredentialImportError,
    DouyinHttpPreflightError,
    DouyinHttpWorkerExited,
    HttpWorkerProcess,
    build_http_credentials_from_storage_state,
    check_douyin_http,
    import_http_credentials_from_browser,
    login_douyin_http,
    redact_secrets,
    resolve_upload_engine,
    validate_http_request,
)
from utils.publish_safety import (
    PublishNeedsHuman,
    PublishOutcomeUnknown,
    PublishRejected,
)

FAKE_WORKER = r'''
import json, sys, time

task = json.load(open(sys.argv[1], encoding="utf-8"))
behavior = task.get("_behavior", "success")

def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)

mode = task["mode"]
if mode == "check":
    if behavior == "check_invalid":
        emit({"event": "result", "mode": "check", "success": False, "valid": False, "message": "invalid"})
    elif behavior == "check_error":
        emit({"event": "error", "stage": "check", "message": "network down"})
        sys.exit(1)
    else:
        emit({"event": "result", "mode": "check", "success": True, "valid": True, "message": "ok"})
    sys.exit(0)

if mode == "login":
    if behavior == "login_qr":
        emit({"event": "qrcode", "url": "https://example.test/qr"})
    if behavior == "login_error":
        emit({"event": "error", "stage": "login", "message": "login failed"})
        sys.exit(1)
    with open(task["credentials_path"], "w", encoding="utf-8") as file_obj:
        json.dump({
            "schema_version": 1,
            "cookie": "sessionid=abc; sid_guard=def",
            "ticket": "ticket",
            "ts_sign": "ts_sign",
            "client_cert": None,
            "private_key": "private",
            "dtrait_blob": None,
            "saved_at": "2026-01-01T00:00:00Z",
        }, file_obj)
    emit({"event": "result", "mode": "login", "success": True, "message": "login completed"})
    sys.exit(0)

if behavior == "pre_error":
    emit({"event": "error", "stage": "pre_submit", "message": "upload failed"})
    sys.exit(1)
if behavior == "exit_before_ready":
    sys.exit(7)
if behavior == "slow_ready":
    time.sleep(5)
    sys.exit(0)

emit({"event": "ready_to_submit"})
line = sys.stdin.readline()
if not line:
    sys.exit(2)
try:
    command = json.loads(line)
except ValueError:
    command = {}
if command.get("cmd") != "submit":
    sys.exit(3)

if behavior == "success":
    emit({"event": "result", "mode": "publish", "success": True, "message": "ok", "item_id": "123", "http_status": 200})
elif behavior == "rejected":
    emit({"event": "result", "mode": "publish", "success": False, "message": "violation", "rejected": True, "stage": "submit", "status_code": 8, "http_status": 200})
elif behavior == "needs_human":
    emit({"event": "result", "mode": "publish", "success": False, "message": "verify", "needs_human": True, "stage": "submit", "verify_decision": "VERIFY", "http_status": 200})
elif behavior == "unknown":
    emit({"event": "result", "mode": "publish", "success": False, "message": "timeout", "stage": "submit_unknown"})
elif behavior == "error_after_submit":
    emit({"event": "error", "stage": "submit", "message": "crashed"})
    sys.exit(1)
elif behavior == "eof":
    sys.exit(9)
else:
    emit({"event": "result", "mode": "publish", "success": True, "message": "ok", "item_id": "1"})
'''


class RedactTests(unittest.TestCase):
    def test_secret_values_are_redacted(self):
        text = 'cookie=sessionid=abc; ticket: "abc"; private_key=xyz'
        redacted = redact_secrets(text)
        self.assertNotIn("sessionid=abc", redacted)
        self.assertNotIn('"abc"', redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("cookie=***", redacted)


class EngineResolutionTests(unittest.TestCase):
    def _request(self, **kwargs):
        options = {
            "engine": "auto",
            "account_name": "ops",
            "visibility": "public",
            "prepare_only": False,
            "declaration": None,
            "product_link": "",
            "product_title": "",
            "collection_name": None,
            "skip_ai_declaration": False,
        }
        options.update(kwargs)
        return types.SimpleNamespace(**options)

    def test_auto_uses_http_when_runtime_and_credentials_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "douyin_http_ops.json"
            credentials.write_text("{}", encoding="utf-8")
            with (
                patch.object(http_engine, "is_runtime_installed", return_value=True),
                patch.object(
                    http_engine, "resolve_http_account_file", return_value=credentials
                ),
            ):
                self.assertEqual(resolve_upload_engine(self._request()), "http")

    def test_auto_falls_back_to_browser_without_runtime(self):
        with patch.object(http_engine, "is_runtime_installed", return_value=False):
            self.assertEqual(resolve_upload_engine(self._request()), "browser")

    def test_skip_declaration_forces_http(self):
        with patch.object(http_engine, "is_runtime_installed", return_value=False):
            self.assertEqual(
                resolve_upload_engine(self._request(skip_ai_declaration=True)),
                "http",
            )

    def test_non_public_visibility_forces_http_without_runtime(self):
        with patch.object(http_engine, "is_runtime_installed", return_value=False):
            for visibility in ("private", "friends"):
                self.assertEqual(
                    resolve_upload_engine(self._request(visibility=visibility)),
                    "http",
                )

    def test_non_public_visibility_rejects_browser_engine(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "浏览器引擎只会公开发布"):
            resolve_upload_engine(
                self._request(engine="browser", visibility="private")
            )

    def test_non_public_visibility_conflicts_with_browser_only_options(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "非公开可见范围"):
            resolve_upload_engine(
                self._request(visibility="friends", prepare_only=True)
            )

    def test_unknown_visibility_is_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "可见范围"):
            resolve_upload_engine(self._request(visibility="unlisted"))

    def test_browser_only_option_wins_over_http(self):
        with patch.object(http_engine, "is_runtime_installed", return_value=True):
            self.assertEqual(
                resolve_upload_engine(
                    self._request(skip_ai_declaration=True, prepare_only=True)
                ),
                "browser",
            )

    def test_explicit_engine_is_returned(self):
        self.assertEqual(resolve_upload_engine(self._request(engine="http")), "http")
        self.assertEqual(resolve_upload_engine(self._request(engine="browser")), "browser")

    def test_invalid_engine_is_rejected(self):
        with self.assertRaises(DouyinHttpPreflightError):
            resolve_upload_engine(self._request(engine="magic"))


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "video.mp4"
        self.video.write_bytes(b"video-bytes")
        self.cover = self.tmp / "cover.jpg"
        self.cover.write_bytes(b"cover-bytes")
        self.credentials = self.tmp / "douyin_http_ops.json"
        self.credentials.write_text(
            json.dumps(
                {
                    "cookie": "sessionid=abc",
                    "ticket": "ticket",
                    "ts_sign": "ts_sign",
                    "private_key": "private",
                }
            ),
            encoding="utf-8",
        )
        self.runtime = self.tmp / "runtime"
        (self.runtime / "src").mkdir(parents=True)
        self._patches = [
            patch.object(http_engine, "validate_runtime", return_value=self.runtime),
            patch.object(
                http_engine,
                "resolve_http_account_file",
                return_value=self.credentials,
            ),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _request(self, **kwargs):
        options = {
            "account_name": "ops",
            "video_file": self.video,
            "title": "高层火灾",
            "description": "往上跑还是往下跑",
            "tags": ["消防", "安全"],
            "publish_date": 0,
            "thumbnail_file": self.cover,
            "skip_ai_declaration": True,
        }
        options.update(kwargs)
        return DouyinVideoUploadRequest(**options)

    def test_valid_request_returns_preflight_values(self):
        runtime, credentials, cover, visibility, timing = validate_http_request(
            self._request()
        )
        self.assertEqual(runtime, self.runtime)
        self.assertEqual(credentials, self.credentials)
        self.assertEqual(cover, self.cover)
        self.assertEqual(visibility, 0)
        self.assertIsNone(timing)

    def test_private_visibility_and_schedule(self):
        future = datetime.now() + timedelta(hours=2)
        _, _, _, visibility, timing = validate_http_request(
            self._request(visibility="private", publish_date=future)
        )
        self.assertEqual(visibility, 1)
        self.assertAlmostEqual(timing, future.timestamp(), places=3)

    def test_prepare_only_is_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "草稿"):
            validate_http_request(self._request(prepare_only=True))

    def test_declaration_is_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "自主声明"):
            validate_http_request(self._request(declaration="内容由AI生成"))

    def test_product_and_collection_are_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "商品"):
            validate_http_request(self._request(product_link="https://x"))
        with self.assertRaisesRegex(DouyinHttpPreflightError, "合集"):
            validate_http_request(self._request(collection_name="系列"))

    def test_skip_declaration_is_required(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "skip-ai-declaration"):
            validate_http_request(self._request(skip_ai_declaration=False))

    def test_missing_cover_is_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "封面"):
            validate_http_request(
                self._request(
                    thumbnail_file=None,
                    thumbnail_landscape_file=None,
                    thumbnail_portrait_file=None,
                )
            )

    def test_title_limit_uses_utf16_units(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "上限"):
            validate_http_request(self._request(title="字" * 21))

    def test_past_schedule_is_rejected(self):
        with self.assertRaisesRegex(DouyinHttpPreflightError, "定时"):
            validate_http_request(
                self._request(publish_date=datetime.now() - timedelta(hours=1))
            )

    def test_missing_credentials_is_rejected(self):
        self.credentials.unlink()
        with self.assertRaisesRegex(DouyinHttpPreflightError, "凭据不存在"):
            validate_http_request(self._request())

    def test_incomplete_credentials_is_rejected(self):
        self.credentials.write_text(
            json.dumps({"cookie": "sessionid=abc", "ticket": ""}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DouyinHttpPreflightError, "缺少字段"):
            validate_http_request(self._request())

    def test_description_appends_hashtags(self):
        self.assertEqual(
            http_engine._build_description("正文", ["a", "b"]), "正文 #a #b"
        )
        self.assertEqual(http_engine._build_description("", ["a"]), "#a")


class WorkerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "fake_worker.py"
        self.script.write_text(FAKE_WORKER, encoding="utf-8")
        self.runtime = self.tmp / "runtime"
        (self.runtime / "src").mkdir(parents=True)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _start(self, behavior, mode="publish"):
        task = {
            "protocol_version": 1,
            "mode": mode,
            "runtime_src": str(self.runtime / "src"),
            "credentials_path": str(self.tmp / "credentials.json"),
            "_behavior": behavior,
        }
        worker = HttpWorkerProcess(
            self.runtime,
            task,
            worker_source=self.script,
            python_executable=Path(sys.executable),
        )
        await worker.start()
        self.addAsyncCleanup(worker.aclose)
        return worker

    async def test_ready_submit_success(self):
        worker = await self._start("success")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        await worker.wait_final(5)
        self.assertEqual(worker.final_result["item_id"], "123")
        self.assertEqual(worker.final_result["http_status"], 200)

    async def test_pre_submit_error(self):
        worker = await self._start("pre_error")
        with self.assertRaises(DouyinHttpPreflightError):
            await worker.wait_ready_or_error(5)

    async def test_worker_exit_before_ready(self):
        worker = await self._start("exit_before_ready")
        with self.assertRaises(DouyinHttpWorkerExited):
            await worker.wait_ready_or_error(5)

    async def test_ready_timeout(self):
        worker = await self._start("slow_ready")
        with self.assertRaises(asyncio.TimeoutError):
            await worker.wait_ready_or_error(0.2)

    async def test_rejected_after_submit(self):
        worker = await self._start("rejected")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        with self.assertRaises(PublishRejected):
            await worker.wait_final(5)

    async def test_needs_human_after_submit(self):
        worker = await self._start("needs_human")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        with self.assertRaises(PublishNeedsHuman):
            await worker.wait_final(5)
        self.assertIn("VERIFY", await worker.risk_signal())

    async def test_unknown_after_submit(self):
        worker = await self._start("unknown")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        with self.assertRaises(PublishOutcomeUnknown):
            await worker.wait_final(5)

    async def test_error_after_submit_is_unknown(self):
        worker = await self._start("error_after_submit")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        with self.assertRaises(PublishOutcomeUnknown):
            await worker.wait_final(5)

    async def test_eof_after_submit_is_worker_exit(self):
        worker = await self._start("eof")
        await worker.wait_ready_or_error(5)
        await worker.send_submit()
        with self.assertRaises(DouyinHttpWorkerExited):
            await worker.wait_final(5)

    async def test_aclose_terminates_slow_worker(self):
        worker = await self._start("slow_ready")
        await worker.aclose()
        self.assertIsNotNone(worker._proc.returncode)


class CheckAndLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "fake_worker.py"
        self.script.write_text(FAKE_WORKER, encoding="utf-8")
        self.runtime = self.tmp / "runtime"
        (self.runtime / "src").mkdir(parents=True)
        self.credentials = self.tmp / "douyin_http_ops.json"
        self.credentials.write_text(
            json.dumps(
                {
                    "cookie": "sessionid=abc",
                    "ticket": "ticket",
                    "ts_sign": "ts_sign",
                    "private_key": "private",
                }
            ),
            encoding="utf-8",
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _factory(self, behavior):
        original = HttpWorkerProcess

        def factory(runtime, task, **kwargs):
            merged = {**task, "_behavior": behavior}
            return original(
                runtime,
                merged,
                worker_source=self.script,
                python_executable=Path(sys.executable),
            )

        return factory

    async def test_check_valid_and_invalid(self):
        for behavior, expected in (("success", True), ("check_invalid", False), ("check_error", False)):
            with (
                patch.object(http_engine, "validate_runtime", return_value=self.runtime),
                patch.object(
                    http_engine,
                    "resolve_http_account_file",
                    return_value=self.credentials,
                ),
                patch.object(
                    http_engine,
                    "HttpWorkerProcess",
                    side_effect=self._factory(behavior),
                ),
            ):
                self.assertEqual(await check_douyin_http("ops"), expected)

    async def test_login_writes_credentials_and_emits_qrcode(self):
        seen = []
        factory = self._factory("login_qr")
        with (
            patch.object(http_engine, "validate_runtime", return_value=self.runtime),
            patch.object(
                http_engine,
                "resolve_http_account_file",
                return_value=self.credentials,
            ),
            patch.object(http_engine, "HttpWorkerProcess", side_effect=factory),
        ):
            result = await login_douyin_http(
                "ops", force=True, on_qrcode=seen.append
            )
        self.assertFalse(result.reused)
        self.assertEqual(seen, ["https://example.test/qr"])
        payload = json.loads(self.credentials.read_text(encoding="utf-8"))
        self.assertEqual(payload["account"], "ops")
        self.assertEqual(payload["platform"], "douyin")
        self.assertEqual(payload["engine"], "http")
        self.assertEqual(payload["ticket"], "ticket")


class StorageStateImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.storage = self.tmp / "storage.json"
        self.account_file = self.tmp / "douyin_http_ops.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_storage(self, *, ticket=True, sessionid=True):
        sdk = {
            "ticket": "hash.test" if ticket else "",
            "ts_sign": "ts.2.test",
            "client_cert": "pub.test",
            "private_key": "private-test",
        }
        cookies = [
            {"name": "sessionid", "value": "session", "domain": ".douyin.com", "expires": -1},
            {"name": "ttwid", "value": "ttwid", "domain": ".douyin.com", "expires": -1},
            {"name": "expired", "value": "old", "domain": ".douyin.com", "expires": 1},
            {"name": "other", "value": "ignored", "domain": "example.com", "expires": -1},
        ]
        if not sessionid:
            cookies.pop(0)
        self.storage.write_text(
            json.dumps(
                {
                    "cookies": cookies,
                    "origins": [
                        {
                            "origin": "https://creator.douyin.com",
                            "localStorage": [
                                {
                                    "name": "security-sdk/s_sdk_sign_data_key/web_protect",
                                    "value": json.dumps({"data": json.dumps(sdk)}),
                                },
                                {
                                    "name": "security-sdk/s_sdk_crypt_sdk",
                                    "value": json.dumps(
                                        {"data": json.dumps({"ec_privateKey": "ec-private-test"})}
                                    ),
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_build_credentials_from_storage_state(self):
        self._write_storage()
        payload = build_http_credentials_from_storage_state(self.storage)
        self.assertEqual(payload["ticket"], "hash.test")
        self.assertEqual(payload["ts_sign"], "ts.2.test")
        self.assertEqual(payload["private_key"], "ec-private-test")
        self.assertEqual(payload["source"], "browser-storage-state")
        self.assertIn("sessionid=session", payload["cookie"])
        self.assertIn("bd_ticket_guard_ts_sign_id=ts.2.test", payload["cookie"])
        self.assertNotIn("expired=old", payload["cookie"])
        self.assertNotIn("other=ignored", payload["cookie"])

    def test_missing_login_or_ticket_is_rejected(self):
        self._write_storage(sessionid=False)
        with self.assertRaises(DouyinHttpCredentialImportError):
            build_http_credentials_from_storage_state(self.storage)
        self._write_storage(ticket=False)
        with self.assertRaises(DouyinHttpCredentialImportError):
            build_http_credentials_from_storage_state(self.storage)

    async def test_import_writes_private_json_and_checks(self):
        self._write_storage()
        checker = AsyncMock(return_value=True)
        result = SimpleNamespace(
            account_file=self.account_file, valid=True
        )
        with (
            patch.object(
                http_engine,
                "resolve_http_account_file",
                return_value=self.account_file,
            ),
            patch.object(http_engine, "check_douyin_http", checker),
        ):
            imported = await import_http_credentials_from_browser(
                "ops", self.storage
            )
        self.assertEqual(imported.account_file, result.account_file)
        self.assertTrue(imported.valid)
        payload = json.loads(self.account_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["account"], "ops")
        self.assertEqual(payload["engine"], "http")
        checker.assert_awaited_once()
        checked_path = checker.await_args.kwargs["credentials_path"]
        self.assertFalse(checked_path.exists())

    async def test_invalid_candidate_does_not_replace_existing_credentials(self):
        self._write_storage()
        self.account_file.write_text("original", encoding="utf-8")
        checker = AsyncMock(return_value=False)
        with (
            patch.object(
                http_engine,
                "resolve_http_account_file",
                return_value=self.account_file,
            ),
            patch.object(http_engine, "check_douyin_http", checker),
        ):
            imported = await import_http_credentials_from_browser(
                "ops", self.storage
            )
        self.assertFalse(imported.valid)
        self.assertEqual(self.account_file.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.tmp.glob("*.import")), [])


class RuntimeStatusTests(unittest.TestCase):
    def test_status_shape(self):
        with patch.object(http_engine, "is_runtime_installed", return_value=False):
            status = http_engine.runtime_status()
        self.assertFalse(status["installed"])
        self.assertIn("runtime_dir", status)


if __name__ == "__main__":
    unittest.main()
