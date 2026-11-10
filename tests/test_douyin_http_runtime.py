# Language: 中文
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from uploader.douyin_uploader import http_runtime
from uploader.douyin_uploader.http_runtime import (
    DOUYIN_SPIDER_COMMIT,
    SOURCE_FILES,
    DouyinHttpRuntimeError,
    DouyinHttpRuntimeInstallError,
    DouyinHttpRuntimeIntegrityError,
    DouyinHttpRuntimeNotInstalled,
    build_manifest,
    clean_external_env,
    ensure_runtime,
    is_runtime_installed,
    runtime_dir,
    runtime_root,
    validate_runtime,
    venv_python,
)


def _fake_runtime(root: Path) -> Path:
    runtime = root / DOUYIN_SPIDER_COMMIT
    src = runtime / "src"
    for relative in SOURCE_FILES:
        path = src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    python_path = venv_python(runtime)
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_bytes(b"python")
    (runtime / "requirements.lock").write_text("curl_cffi==0.16.0\n", encoding="utf-8")
    manifest = build_manifest(runtime, source="dir", archive_sha256=None)
    (runtime / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return runtime


class SelfTestImportOrderTests(unittest.TestCase):
    def test_douyin_api_import_precedes_builder_auth(self):
        code = http_runtime._SELF_TEST_CODE
        self.assertLess(
            code.index("import dy_apis.douyin_api"),
            code.index("from builder.auth import DouyinAuth"),
        )


class RuntimePathTests(unittest.TestCase):
    def test_env_override_and_commit_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SAU_DOUYIN_HTTP_ROOT": temp_dir}):
                self.assertEqual(runtime_root(), Path(temp_dir))
                self.assertEqual(
                    runtime_dir(DOUYIN_SPIDER_COMMIT),
                    Path(temp_dir) / DOUYIN_SPIDER_COMMIT,
                )
                with self.assertRaises(ValueError):
                    runtime_dir("../evil")

    def test_conf_override_used_when_env_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_conf = type("FakeConf", (), {"DOUYIN_HTTP_ROOT": temp_dir})
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.dict("sys.modules", {"conf": fake_conf}),
            ):
                os.environ.pop("SAU_DOUYIN_HTTP_ROOT", None)
                self.assertEqual(runtime_root(), Path(temp_dir))

    def test_clean_env_drops_pythonpath_and_dy_credentials(self):
        with patch.dict(
            os.environ,
            {
                "PYTHONPATH": "C:/repo",
                "DY_TICKET": "secret",
                "DY_COOKIES": "secret",
                "KEEP_ME": "1",
            },
            clear=False,
        ):
            env = clean_external_env()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("DY_TICKET", env)
        self.assertNotIn("DY_COOKIES", env)
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")


class ValidateRuntimeTests(unittest.TestCase):
    def test_valid_runtime_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _fake_runtime(Path(temp_dir))
            self.assertTrue(is_runtime_installed(runtime))
            self.assertEqual(validate_runtime(runtime), runtime)

    def test_missing_manifest_is_not_installed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / DOUYIN_SPIDER_COMMIT
            runtime.mkdir(parents=True)
            self.assertFalse(is_runtime_installed(runtime))
            with self.assertRaises(DouyinHttpRuntimeNotInstalled):
                validate_runtime(runtime)

    def test_tampered_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _fake_runtime(Path(temp_dir))
            target = runtime / "src" / SOURCE_FILES[0]
            target.write_text("# tampered\n", encoding="utf-8")
            with self.assertRaises(DouyinHttpRuntimeIntegrityError):
                validate_runtime(runtime)

    def test_commit_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _fake_runtime(Path(temp_dir))
            manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
            manifest["commit"] = "0" * 40
            (runtime / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(DouyinHttpRuntimeIntegrityError):
                validate_runtime(runtime)

    def test_tampered_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _fake_runtime(Path(temp_dir))
            (runtime / "requirements.lock").write_text("evil\n", encoding="utf-8")
            with self.assertRaises(DouyinHttpRuntimeIntegrityError):
                validate_runtime(runtime)


class ZipExtractionTests(unittest.TestCase):
    def _zip(self, root: Path, members: dict[str, bytes]) -> Path:
        path = root / "source.zip"
        with zipfile.ZipFile(path, "w") as zip_file:
            for name, data in members.items():
                zip_file.writestr(name, data)
        return path

    def test_normal_archive_returns_single_top_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = self._zip(
                root,
                {
                    "repo-abc/requirements.txt": b"x",
                    "repo-abc/dy_apis/douyin_creator_api.py": b"y",
                },
            )
            extract = root / "extract"
            extract.mkdir()
            top = http_runtime._safe_extract_zip(archive, extract)
            self.assertEqual(top.name, "repo-abc")
            self.assertTrue((top / "requirements.txt").is_file())

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = self._zip(root, {"repo-abc/../../evil.txt": b"x"})
            extract = root / "extract"
            extract.mkdir()
            with self.assertRaises(DouyinHttpRuntimeIntegrityError):
                http_runtime._safe_extract_zip(archive, extract)
            self.assertFalse((root / "evil.txt").exists())

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = self._zip(root, {"/evil.txt": b"x"})
            extract = root / "extract"
            extract.mkdir()
            with self.assertRaises(DouyinHttpRuntimeIntegrityError):
                http_runtime._safe_extract_zip(archive, extract)


class CopySourceTests(unittest.TestCase):
    def _git_repo(self, root: Path) -> Path:
        repo = root / "checkout"
        repo.mkdir()
        (repo / "requirements.txt").write_text("x\n", encoding="utf-8")
        (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-qm",
                "init",
            ],
            cwd=repo,
            check=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return repo, head

    def test_copy_excludes_env_and_requires_matching_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, head = self._git_repo(root)
            destination = root / "staged"
            http_runtime._copy_source_dir(repo, destination, commit=head)
            self.assertTrue((destination / "requirements.txt").is_file())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / ".git").exists())

            with self.assertRaises(DouyinHttpRuntimeInstallError):
                http_runtime._copy_source_dir(repo, root / "staged-bad", commit="0" * 40)


class EnsureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(
            os.environ, {"SAU_DOUYIN_HTTP_ROOT": str(self.tmp / "tools")}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_accept_third_party_is_required(self):
        with self.assertRaises(DouyinHttpRuntimeInstallError):
            ensure_runtime(False)

    def test_existing_runtime_is_reused_without_reinstall(self):
        runtime = _fake_runtime(self.tmp / "tools")
        with patch.object(
            http_runtime, "_prepare_source", side_effect=AssertionError("must not reinstall")
        ):
            self.assertEqual(ensure_runtime(True), runtime)

    def test_install_flow_writes_manifest_and_reuses_it(self):
        calls = {"prepare": 0}

        def fake_prepare(source_dir, destination, *, commit, timeout):
            calls["prepare"] += 1
            for relative in SOURCE_FILES:
                path = destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {relative}\n", encoding="utf-8")
            return {"source": "dir", "archive_sha256": None}

        def fake_create_venv(runtime, timeout):
            python_path = venv_python(runtime)
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_bytes(b"python")
            return python_path

        def fake_freeze(python_path, lock_path, timeout):
            lock_path.write_text("curl_cffi==0.16.0\n", encoding="utf-8")

        with (
            patch.object(http_runtime, "_prepare_source", side_effect=fake_prepare),
            patch.object(http_runtime, "_create_venv", side_effect=fake_create_venv),
            patch.object(
                http_runtime, "_install_requirements", return_value=(True, "")
            ),
            patch.object(http_runtime, "_freeze", side_effect=fake_freeze),
            patch.object(http_runtime, "_self_test"),
        ):
            runtime = ensure_runtime(True)
            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertTrue(is_runtime_installed(runtime))

            # 已安装且未 --force：直接复用，不重新准备源码。
            ensure_runtime(True)
            self.assertEqual(calls["prepare"], 1)

            # --force：重新走安装流程。
            ensure_runtime(True, force=True)
            self.assertEqual(calls["prepare"], 2)

    def test_failed_install_leaves_marker(self):
        runtime = runtime_dir()
        with patch.object(
            http_runtime,
            "_prepare_source",
            side_effect=DouyinHttpRuntimeInstallError("download failed"),
        ):
            with self.assertRaises(DouyinHttpRuntimeInstallError):
                ensure_runtime(True)

        marker = runtime / ".failed"
        self.assertTrue(marker.is_file())
        self.assertIn("download failed", marker.read_text(encoding="utf-8"))
        self.assertFalse(is_runtime_installed(runtime))


class DescriptionTests(unittest.TestCase):
    def test_describe_runtime_reports_missing_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SAU_DOUYIN_HTTP_ROOT": temp_dir}):
                info = http_runtime.describe_runtime()
        self.assertFalse(info["installed"])
        self.assertEqual(info["commit"], DOUYIN_SPIDER_COMMIT)
        self.assertIsNone(info["manifest"])


if __name__ == "__main__":
    unittest.main()
