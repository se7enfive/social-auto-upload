# Language: 中文
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.douyin_uploader import http_worker


class LazyImportOrderTests(unittest.TestCase):
    def test_load_douyin_auth_imports_douyin_api_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "dy_apis").mkdir()
            (root / "builder").mkdir()
            (root / "dy_apis" / "__init__.py").write_text("", encoding="utf-8")
            (root / "builder" / "__init__.py").write_text("", encoding="utf-8")
            (root / "dy_apis" / "douyin_api.py").write_text(
                "from builder.auth import DouyinAuth\nAPI_READY = True\n",
                encoding="utf-8",
            )
            (root / "builder" / "auth.py").write_text(
                "from dy_apis import douyin_api\n"
                "class DouyinAuth:\n"
                "    @staticmethod\n"
                "    def from_cookie(*args, **kwargs):\n"
                "        return object()\n",
                encoding="utf-8",
            )
            tracked = (
                "dy_apis",
                "dy_apis.douyin_api",
                "builder",
                "builder.auth",
            )
            saved = {name: sys.modules.get(name) for name in tracked}
            sys.path.insert(0, str(root))
            try:
                for name in tracked:
                    sys.modules.pop(name, None)
                cls = http_worker._load_douyin_auth()
                self.assertTrue(callable(cls.from_cookie))
            finally:
                sys.path.remove(str(root))
                for name, module in saved.items():
                    if module is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = module


class PublishResultTests(unittest.TestCase):
    def _gate(self, **kwargs):
        gate = http_worker.SubmitGate(io.StringIO())
        for key, value in kwargs.items():
            setattr(gate, key, value)
        return gate

    def test_success_requires_item_id(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=200,
        )
        result = http_worker._publish_result(
            gate, True, "ok", {"status_code": 0, "item_id": "123"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "123")
        self.assertEqual(result["http_status"], 200)

    def test_pre_submit_failure_is_not_unknown(self):
        gate = self._gate(create_started=False)
        result = http_worker._publish_result(gate, False, "cover upload failed", None)
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "pre_submit")

    def test_second_factor_is_needs_human(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=200,
            verify_decision="VERIFY",
        )
        result = http_worker._publish_result(
            gate, False, "verify", {"status_code": 4}
        )
        self.assertEqual(result["stage"], "submit")
        self.assertTrue(result["needs_human"])
        self.assertNotIn("rejected", result)

    def test_json_status_code_marks_rejected(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=200,
        )
        result = http_worker._publish_result(
            gate, False, "内容违规", {"status_code": 8, "status_msg": "违规"}
        )
        self.assertTrue(result["rejected"])
        self.assertEqual(result["status_code"], 8)

    def test_network_failure_after_send_is_unknown(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=False,
            transport_error="TimeoutError: read timed out",
        )
        result = http_worker._publish_result(gate, False, "timeout", None)
        self.assertEqual(result["stage"], "submit_unknown")
        self.assertIn("TimeoutError", result["transport_error"])

    def test_http_403_without_json_is_rejected(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=403,
        )
        result = http_worker._publish_result(gate, False, "403", None)
        self.assertTrue(result["rejected"])

    def test_http_500_without_json_is_unknown(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=502,
        )
        result = http_worker._publish_result(gate, False, "502", None)
        self.assertEqual(result["stage"], "submit_unknown")

    def test_zero_status_without_item_id_is_unknown(self):
        gate = self._gate(
            create_started=True,
            submit_sent=True,
            response_received=True,
            http_status=200,
        )
        result = http_worker._publish_result(
            gate, False, "no item id", {"status_code": 0}
        )
        self.assertEqual(result["stage"], "submit_unknown")

    def test_pass_decision_does_not_trigger_human(self):
        self.assertFalse(http_worker._needs_human("PASS"))
        self.assertFalse(http_worker._needs_human("0"))
        self.assertFalse(http_worker._needs_human(None))
        self.assertTrue(http_worker._needs_human("VERIFY"))
        self.assertTrue(http_worker._needs_human("need_sms"))


class SubmitGateTests(unittest.TestCase):
    def _fake_api(self):
        package = types.ModuleType("dy_apis")
        module = types.ModuleType("dy_apis.douyin_creator_api")
        module.calls = []

        def original(auth, method, url, **kwargs):
            module.calls.append((method, url, kwargs))
            return types.SimpleNamespace(
                status_code=200,
                headers={"x-tt-verify-passport-decision": "PASS"},
            )

        module._request = original
        package.douyin_creator_api = module
        return package, module

    def test_create_v2_waits_for_submit_command(self):
        package, module = self._fake_api()
        protocol = io.StringIO()
        gate = http_worker.SubmitGate(protocol)
        with (
            patch.dict(
                sys.modules,
                {
                    "dy_apis": package,
                    "dy_apis.douyin_creator_api": module,
                },
            ),
            patch.object(sys, "stdin", io.StringIO('{"cmd":"submit"}\n')),
        ):
            gate.install()
            response = module._request(
                None,
                "POST",
                "https://creator.douyin.com/web/api/media/aweme/create_v2/",
                data=b"{}",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(module.calls), 1)
        self.assertTrue(gate.create_started)
        self.assertTrue(gate.submit_sent)
        self.assertTrue(gate.response_received)
        self.assertEqual(gate.verify_decision, "PASS")
        events = [json.loads(line) for line in protocol.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "ready_to_submit")

    def test_no_submit_command_never_sends_request(self):
        package, module = self._fake_api()
        protocol = io.StringIO()
        gate = http_worker.SubmitGate(protocol)
        with (
            patch.dict(
                sys.modules,
                {
                    "dy_apis": package,
                    "dy_apis.douyin_creator_api": module,
                },
            ),
            patch.object(sys, "stdin", io.StringIO("")),
        ):
            gate.install()
            with self.assertRaises(RuntimeError):
                module._request(
                    None,
                    "POST",
                    "https://creator.douyin.com/web/api/media/aweme/create_v2/",
                )
        self.assertEqual(module.calls, [])
        self.assertTrue(gate.create_started)
        self.assertFalse(gate.submit_sent)
        self.assertIn("未确认提交", gate.transport_error)

    def test_second_create_v2_call_is_blocked(self):
        package, module = self._fake_api()
        protocol = io.StringIO()
        gate = http_worker.SubmitGate(protocol)
        with (
            patch.dict(
                sys.modules,
                {
                    "dy_apis": package,
                    "dy_apis.douyin_creator_api": module,
                },
            ),
            patch.object(sys, "stdin", io.StringIO('{"cmd":"submit"}\n')),
        ):
            gate.install()
            url = "https://creator.douyin.com/web/api/media/aweme/create_v2/"
            module._request(None, "POST", url)
            with self.assertRaises(RuntimeError):
                module._request(None, "POST", url)
        self.assertEqual(len(module.calls), 1)

    def test_non_create_requests_bypass_gate(self):
        package, module = self._fake_api()
        gate = http_worker.SubmitGate(io.StringIO())
        with patch.dict(
            sys.modules,
            {
                "dy_apis": package,
                "dy_apis.douyin_creator_api": module,
            },
        ):
            gate.install()
            module._request(None, "GET", "https://creator.douyin.com/other")
        self.assertEqual(len(module.calls), 1)
        self.assertFalse(gate.create_started)


class CredentialTests(unittest.TestCase):
    def test_write_credentials_is_atomic_and_requires_core_fields(self):
        auth = types.SimpleNamespace(
            cookie_str="sessionid=abc; sid_guard=def",
            ticket="ticket",
            ts_sign="ts_sign",
            client_cert=None,
            private_key="private",
            dtrait_blob=None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "douyin_http_ops.json"
            payload = http_worker._write_credentials(target, auth)
            self.assertEqual(payload["cookie"], auth.cookie_str)
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(stored["ticket"], "ticket")
            self.assertEqual(stored["private_key"], "private")
            self.assertIsNone(stored["client_cert"])

    def test_missing_required_credential_is_rejected(self):
        auth = types.SimpleNamespace(
            cookie_str="sessionid=abc",
            ticket=None,
            ts_sign=None,
            client_cert=None,
            private_key=None,
            dtrait_blob=None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                http_worker._write_credentials(Path(temp_dir) / "bad.json", auth)


class TaskTests(unittest.TestCase):
    def test_protocol_version_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task.json"
            path.write_text(
                json.dumps({"protocol_version": 2, "mode": "check"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                http_worker._load_task(str(path))


if __name__ == "__main__":
    unittest.main()
