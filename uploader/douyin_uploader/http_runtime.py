"""DouYin_Spider 外部运行时：固定 commit、独立 venv、用户目录缓存。

第三方仓库没有 LICENSE，因此不把源码、归档或 venv 放进本仓库；这里只负责：

- 从固定 commit 的 GitHub archive 或本地 checkout 安装到用户目录；
- 建独立 venv 并安装发布所需依赖；
- 写入 manifest（源文件哈希 + 依赖锁），之后每次运行前校验完整性；
- 用一次无凭据 self-test 确认关键入口仍存在。

安装目录：``<root>/<commit>/{src,venv,manifest.json,requirements.lock}``。
root 可由 ``SAU_DOUYIN_HTTP_ROOT`` 或 ``conf.DOUYIN_HTTP_ROOT`` 覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

DOUYIN_SPIDER_REPO = "https://github.com/cv-cat/DouYin_Spider"
DOUYIN_SPIDER_COMMIT = "d151f9c2980abc304c1ae4918c2272675cd9e4c2"
DOUYIN_SPIDER_ARCHIVE_SHA256 = (
    "3b49dcd40e720d3bb57078bf614cbda5c7db8e8cbc2fcd2a96333102793e7e64"
)
DOUYIN_SPIDER_ARCHIVE_URL = (
    f"{DOUYIN_SPIDER_REPO}/archive/{DOUYIN_SPIDER_COMMIT}.zip"
)

RUNTIME_SCHEMA_VERSION = 1
SOURCE_FILES = (
    "dy_apis/douyin_creator_api.py",
    "builder/auth.py",
    "quick_publish.py",
    "requirements.txt",
)
_COPY_EXCLUDES = (
    ".git",
    ".env",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "*.pyc",
)

# 完整 requirements.txt 里的 opencv / av / rocksdict 等只服务于爬虫与封面生成；
# 发布链路显式传封面、只走 HTTP，不需要它们。完整安装失败时退回这一组。
MINIMAL_REQUIREMENTS = (
    "curl_cffi",
    "cryptography",
    "ecdsa",
    "loguru",
    "python-dotenv",
    "lxml",
    "protobuf>=5.27",
    "pillow",
    "qrcode",
    "backports.zstd ; python_version<'3.14'",
)

_STRIPPED_ENV_PREFIXES = ("DY_",)
_STRIPPED_ENV_NAMES = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "VIRTUAL_ENV",
    }
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class DouyinHttpRuntimeError(RuntimeError):
    """运行时安装或校验失败。"""


class DouyinHttpRuntimeNotInstalled(DouyinHttpRuntimeError):
    """运行时尚未安装。"""


class DouyinHttpRuntimeIntegrityError(DouyinHttpRuntimeError):
    """运行时代码或依赖与 manifest 不一致。"""


class DouyinHttpRuntimeInstallError(DouyinHttpRuntimeError):
    """安装过程失败。"""


def runtime_root() -> Path:
    override = os.environ.get("SAU_DOUYIN_HTTP_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        import conf
    except Exception:
        conf = None
    configured = str(getattr(conf, "DOUYIN_HTTP_ROOT", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".social-auto-upload" / "tools" / "douyin-http"


def runtime_dir(commit: str = DOUYIN_SPIDER_COMMIT) -> Path:
    if not _COMMIT_RE.fullmatch(str(commit)):
        raise ValueError(f"非法 DouYin_Spider commit: {commit!r}")
    return runtime_root() / commit


def venv_python(runtime: str | Path) -> Path:
    root = Path(runtime)
    if os.name == "nt":
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python"


def clean_external_env() -> dict[str, str]:
    """构造子进程环境：不继承本仓库的 PYTHONPATH，也不继承任何 DY_* 凭据。"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _STRIPPED_ENV_NAMES:
            continue
        if any(upper.startswith(prefix) for prefix in _STRIPPED_ENV_PREFIXES):
            continue
        env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(runtime: str | Path) -> dict | None:
    path = Path(runtime) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def validate_runtime(
    runtime: str | Path | None = None, *, commit: str = DOUYIN_SPIDER_COMMIT
) -> Path:
    """校验 manifest、venv 与关键源码哈希；任何不一致都拒绝运行。"""
    root = Path(runtime) if runtime is not None else runtime_dir(commit)
    manifest = read_manifest(root)
    if manifest is None:
        raise DouyinHttpRuntimeNotInstalled(
            f"未找到有效的 DouYin_Spider 运行时：{root}/manifest.json。"
            "请先运行 `sau douyin setup --accept-third-party`。"
        )
    if manifest.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise DouyinHttpRuntimeIntegrityError("运行时 manifest 版本不兼容，请重新 setup")
    if manifest.get("commit") != commit:
        raise DouyinHttpRuntimeIntegrityError(
            f"运行时 commit 不匹配（manifest={manifest.get('commit')!r}, "
            f"expected={commit!r}），请重新 setup"
        )
    if not venv_python(root).is_file():
        raise DouyinHttpRuntimeIntegrityError(
            f"运行时 venv 缺失：{venv_python(root)}，请重新 setup"
        )

    source_hashes = manifest.get("source_files")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise DouyinHttpRuntimeIntegrityError("运行时 manifest 缺少 source_files")
    src = root / "src"
    for relative in SOURCE_FILES:
        expected = source_hashes.get(relative)
        file_path = src / relative
        if not expected or not file_path.is_file():
            raise DouyinHttpRuntimeIntegrityError(
                f"运行时源文件缺失或未登记：{relative}，请重新 setup"
            )
        if sha256_file(file_path) != expected:
            raise DouyinHttpRuntimeIntegrityError(
                f"运行时源文件被修改：{relative}，请重新 setup"
            )

    lock_path = root / "requirements.lock"
    lock_hash = manifest.get("requirements_lock_sha256")
    if not lock_path.is_file() or not lock_hash:
        raise DouyinHttpRuntimeIntegrityError("运行时依赖锁缺失，请重新 setup")
    if sha256_file(lock_path) != lock_hash:
        raise DouyinHttpRuntimeIntegrityError("运行时依赖锁被修改，请重新 setup")
    return root


def is_runtime_installed(
    runtime: str | Path | None = None, *, commit: str = DOUYIN_SPIDER_COMMIT
) -> bool:
    try:
        validate_runtime(runtime, commit=commit)
    except DouyinHttpRuntimeError:
        return False
    return True


def _assert_archive_hash(archive: Path) -> None:
    actual = sha256_file(archive)
    if actual != DOUYIN_SPIDER_ARCHIVE_SHA256:
        raise DouyinHttpRuntimeIntegrityError(
            f"DouYin_Spider 归档校验失败：期望 {DOUYIN_SPIDER_ARCHIVE_SHA256}，"
            f"实际 {actual}"
        )


def _download_archive(destination: Path, timeout: float) -> None:
    request = urllib.request.Request(
        DOUYIN_SPIDER_ARCHIVE_URL,
        headers={"User-Agent": "social-auto-upload"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(destination, "wb") as file_obj:
                shutil.copyfileobj(response, file_obj, length=1024 * 1024)
    except Exception as exc:
        raise DouyinHttpRuntimeInstallError(
            f"下载 DouYin_Spider 归档失败：{exc}。"
            "可改用 `--source <本地 checkout>` 安装。"
        ) from exc


def _safe_extract_zip(archive: Path, destination: Path) -> Path:
    """拒绝绝对路径、``..``、盘符和符号链接，返回唯一顶层目录。"""
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as zip_file:
        bad_member = zip_file.testzip()
        if bad_member:
            raise DouyinHttpRuntimeIntegrityError(f"归档损坏：{bad_member}")
        for info in zip_file.infolist():
            raw_name = info.filename.replace("\\", "/")
            parts = PurePosixPath(raw_name).parts
            if (
                not parts
                or raw_name.startswith("/")
                or ".." in parts
                or ":" in parts[0]
            ):
                raise DouyinHttpRuntimeIntegrityError(
                    f"归档包含非法路径：{info.filename!r}"
                )
            target = (destination / raw_name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise DouyinHttpRuntimeIntegrityError(
                    f"归档路径越界：{info.filename!r}"
                ) from exc
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                raise DouyinHttpRuntimeIntegrityError(
                    f"归档包含符号链接：{info.filename!r}"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(info) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise DouyinHttpRuntimeIntegrityError(
            "归档结构异常：期望唯一顶层目录，实际 " + repr([p.name for p in entries])
        )
    return entries[0]


def _git_head(source_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DouyinHttpRuntimeInstallError(
            f"无法读取 {source_dir} 的 git HEAD：{exc}"
        ) from exc
    if result.returncode != 0:
        raise DouyinHttpRuntimeInstallError(
            f"{source_dir} 不是可用的 git checkout：{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _copy_source_dir(source_dir: Path, destination: Path, *, commit: str) -> None:
    source_dir = Path(source_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise DouyinHttpRuntimeInstallError(f"--source 目录不存在：{source_dir}")
    actual = _git_head(source_dir)
    if actual != commit:
        raise DouyinHttpRuntimeInstallError(
            f"--source HEAD 为 {actual}，与固定 commit {commit} 不一致"
        )
    shutil.copytree(
        source_dir,
        destination,
        ignore=shutil.ignore_patterns(*_COPY_EXCLUDES),
    )


def _prepare_source(
    source_dir: str | Path | None,
    destination: Path,
    *,
    commit: str,
    timeout: float,
) -> dict:
    if source_dir:
        _copy_source_dir(Path(source_dir), destination, commit=commit)
        return {"source": "dir", "archive_sha256": None}

    with tempfile.TemporaryDirectory(prefix="douyin-spider-download-") as temp_dir:
        archive = Path(temp_dir) / "source.zip"
        _download_archive(archive, timeout)
        _assert_archive_hash(archive)
        extract_root = Path(temp_dir) / "extract"
        extract_root.mkdir()
        top_level = _safe_extract_zip(archive, extract_root)
        shutil.move(str(top_level), str(destination))
    return {
        "source": "archive",
        "archive_sha256": DOUYIN_SPIDER_ARCHIVE_SHA256,
    }


def _run_streaming(
    command: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict | None = None,
    progress=None,
) -> tuple[int, str]:
    """运行子进程并流式转发输出；返回 (returncode, 输出尾部)。"""
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail: list[str] = []
    lock = threading.Lock()

    def _reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            with lock:
                tail.append(text)
                if len(tail) > 120:
                    del tail[:-120]
            if progress is not None:
                progress(text)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process)
        reader.join(timeout=5)
        raise DouyinHttpRuntimeInstallError(
            f"命令超时（{timeout:g}s）：{' '.join(command[:4])} …"
        ) from exc
    finally:
        if process.stdout is not None:
            process.stdout.close()
    reader.join(timeout=5)
    with lock:
        return returncode, "\n".join(tail[-40:])


def terminate_process_tree(process: subprocess.Popen, timeout: float = 10.0) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), 15)  # type: ignore[attr-defined]
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _create_venv(runtime: Path, timeout: float) -> Path:
    venv_dir = runtime / "venv"
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise DouyinHttpRuntimeInstallError(
            f"创建独立 venv 失败：{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    python_path = venv_python(runtime)
    if not python_path.is_file():
        raise DouyinHttpRuntimeInstallError(f"venv 创建后缺少 Python：{python_path}")
    return python_path


def _install_requirements(
    python_path: Path,
    requirements: Path,
    *,
    timeout: float,
    progress,
) -> tuple[bool, str]:
    returncode, tail = _run_streaming(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(requirements),
        ],
        timeout=timeout,
        env=clean_external_env(),
        progress=progress,
    )
    return returncode == 0, tail


def _freeze(python_path: Path, lock_path: Path, timeout: float) -> None:
    result = subprocess.run(
        [str(python_path), "-m", "pip", "freeze", "--disable-pip-version-check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=clean_external_env(),
    )
    if result.returncode != 0:
        raise DouyinHttpRuntimeInstallError(
            f"pip freeze 失败：{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    lock_path.write_text(result.stdout, encoding="utf-8")


_SELF_TEST_CODE = """
import sys
sys.path.insert(0, sys.argv[1])
# builder.auth 与 dy_apis.douyin_api 互相导入；必须先导入 douyin_api，
# 否则会得到 partially initialized module 的 ImportError。
import dy_apis.douyin_api  # noqa: F401
from builder.auth import DouyinAuth
from dy_apis import douyin_creator_api
from dy_apis.douyin_creator_api import DouyinCreatorAPI
assert callable(DouyinAuth.from_cookie)
assert callable(DouyinAuth.from_qrcode_login)
assert callable(DouyinCreatorAPI.post_video)
assert callable(douyin_creator_api._request)
print("SELF_TEST_OK")
"""


def _self_test(runtime: Path, *, timeout: float, progress=None) -> None:
    with tempfile.TemporaryDirectory(prefix="douyin-spider-self-test-") as temp_dir:
        result = subprocess.run(
            [
                str(venv_python(runtime)),
                "-I",
                "-c",
                _SELF_TEST_CODE,
                str(runtime / "src"),
            ],
            cwd=temp_dir,
            env=clean_external_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        if progress is not None:
            progress(detail)
        raise DouyinHttpRuntimeInstallError(
            "DouYin_Spider self-test 失败：关键入口无法导入。\n" + detail
        )


def build_manifest(runtime: Path, *, source: str, archive_sha256: str | None) -> dict:
    src = runtime / "src"
    source_files = {relative: sha256_file(src / relative) for relative in SOURCE_FILES}
    lock_path = runtime / "requirements.lock"
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "tool": "DouYin_Spider",
        "commit": DOUYIN_SPIDER_COMMIT,
        "source": source,
        "archive_sha256": archive_sha256,
        "source_files": source_files,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "requirements_lock_sha256": sha256_file(lock_path),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def ensure_runtime(
    accept_third_party: bool,
    *,
    source_dir: str | Path | None = None,
    force: bool = False,
    timeout: float = 900,
    progress=None,
    commit: str = DOUYIN_SPIDER_COMMIT,
) -> Path:
    if not accept_third_party:
        raise DouyinHttpRuntimeInstallError(
            "安装 DouYin_Spider 需要显式接受第三方代码风险："
            "请加 `--accept-third-party`（该仓库没有 LICENSE）。"
        )
    log = progress or print
    log(
        "DouYin_Spider 是第三方、无 LICENSE 的私有接口实现；"
        f"将固定 commit {commit} 安装到用户目录并在运行时调用。"
    )

    runtime = runtime_dir(commit)
    if not force and is_runtime_installed(runtime, commit=commit):
        log(f"DouYin_Spider 运行时已就绪：{runtime}")
        return runtime

    if runtime.exists():
        log(f"重建运行时目录：{runtime}")
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    failed_path = runtime / ".failed"

    try:
        log("准备 DouYin_Spider 源码…")
        source_info = _prepare_source(
            source_dir, runtime / "src", commit=commit, timeout=timeout
        )

        log("创建独立 venv…")
        python_path = _create_venv(runtime, timeout)

        log("安装依赖（首次安装可能较慢）…")
        ok, tail = _install_requirements(
            python_path, runtime / "src" / "requirements.txt", timeout=timeout, progress=log
        )
        if not ok:
            log("完整依赖安装失败，改用最小发布依赖集重试…")
            minimal = runtime / "requirements.minimal.txt"
            minimal.write_text("\n".join(MINIMAL_REQUIREMENTS) + "\n", encoding="utf-8")
            ok, tail = _install_requirements(
                python_path, minimal, timeout=timeout, progress=log
            )
        if not ok:
            raise DouyinHttpRuntimeInstallError(
                "依赖安装失败。pip 输出尾部：\n" + tail
            )

        log("生成依赖锁…")
        _freeze(python_path, runtime / "requirements.lock", timeout)

        log("运行 self-test…")
        _self_test(runtime, timeout=min(timeout, 180), progress=log)

        manifest = build_manifest(
            runtime,
            source=source_info["source"],
            archive_sha256=source_info.get("archive_sha256"),
        )
        _write_json_atomic(runtime / "manifest.json", manifest)
        failed_path.unlink(missing_ok=True)
        log(f"DouYin_Spider 运行时安装完成：{runtime}")
        return runtime
    except Exception as exc:
        try:
            failed_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except OSError:
            pass
        raise


def describe_runtime(commit: str = DOUYIN_SPIDER_COMMIT) -> dict:
    """供 CLI/文档展示运行时状态；不存在时返回 installed=False。"""
    runtime = runtime_dir(commit)
    manifest = read_manifest(runtime)
    return {
        "commit": commit,
        "runtime_dir": str(runtime),
        "installed": is_runtime_installed(runtime, commit=commit),
        "manifest": manifest,
    }
