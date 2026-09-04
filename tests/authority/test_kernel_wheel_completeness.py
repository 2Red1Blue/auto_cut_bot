from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.authority.common import sha256_bytes
from tools.authority.consumer_lock import _verify_wheel

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "packages/autocut-kernel"
_TIMEOUT_SECONDS = 120


def _run(*command: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
    )


def _extract_archive(destination: Path) -> Path:
    commit = _run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip().decode("ascii")
    archive = _run("git", "archive", "--format=tar", commit, "packages/autocut-kernel", cwd=ROOT).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source.getmembers():
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise AssertionError(f"archive path escaped root: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = source.extractfile(member)
                assert payload is not None
                target.write_bytes(payload.read())
            else:
                raise AssertionError(f"archive has non-regular member: {member.name}")
    return destination / "packages/autocut-kernel"


def _package_bytes(source: Path) -> dict[str, bytes]:
    package = source / "src/autocut_kernel"
    assert package.is_dir()
    files = sorted(path for path in package.rglob("*") if path.is_file())
    assert all(not path.is_symlink() for path in files)
    return {path.relative_to(package).as_posix(): path.read_bytes() for path in files}


def _build(source: Path, destination: Path, *, sdist: bool = False) -> Path:
    uv = shutil.which("uv")
    assert uv is not None
    arguments = [uv, "build", "--offline", "--no-build-logs", "--out-dir", str(destination)]
    arguments.append("--sdist" if sdist else "--wheel")
    arguments.append(str(source))
    _run(*arguments, cwd=destination.parent)
    artifacts = list(destination.glob("*.tar.gz" if sdist else "*.whl"))
    assert len(artifacts) == 1
    return artifacts[0]


def _sdist_package_bytes(sdist: Path) -> dict[str, bytes]:
    with tarfile.open(sdist) as source:
        files = [member for member in source.getmembers() if member.isfile()]
        roots = {Path(member.name).parts[0] for member in files}
        assert len(roots) == 1
        prefix = f"{roots.pop()}/src/autocut_kernel/"
        package: dict[str, bytes] = {}
        for member in files:
            if member.name.startswith(prefix):
                stream = source.extractfile(member)
                assert stream is not None
                package[member.name.removeprefix(prefix)] = stream.read()
        return package


@dataclass(frozen=True)
class _WheelEvidence:
    direct_wheels: tuple[Path, Path]
    sdist_wheel: Path
    expected_package: dict[str, bytes]
    version: str


@pytest.fixture(scope="module")
def wheel_evidence(tmp_path_factory: pytest.TempPathFactory) -> _WheelEvidence:
    root = tmp_path_factory.mktemp("kernel-wheel")
    source = _extract_archive(root / "archive")
    expected_package = _package_bytes(source)
    shutil.copyfile(KERNEL / "pyproject.toml", source / "pyproject.toml")
    version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert type(version) is str
    first_source = root / "direct-source-one"
    second_source = root / "direct-source-two"
    shutil.copytree(source, first_source)
    shutil.copytree(source, second_source)
    first = _build(first_source, root / "direct-one")
    second = _build(second_source, root / "direct-two")
    sdist = _build(source, root / "sdist", sdist=True)
    assert _sdist_package_bytes(sdist) == expected_package
    return _WheelEvidence(
        (first, second), _build(sdist, root / "from-sdist"), expected_package, version
    )


def test_archive_overlay_wheels_are_reproducible_and_exact(
    wheel_evidence: _WheelEvidence,
) -> None:
    first, second = wheel_evidence.direct_wheels
    assert sha256_bytes(first.read_bytes()) == sha256_bytes(second.read_bytes())
    _verify_wheel(
        wheel_path=first,
        distribution_version=wheel_evidence.version,
        committed_package_files=wheel_evidence.expected_package,
    )


def test_sdist_wheel_preserves_the_archive_package_bytes(wheel_evidence: _WheelEvidence) -> None:
    _verify_wheel(
        wheel_path=wheel_evidence.sdist_wheel,
        distribution_version=wheel_evidence.version,
        committed_package_files=wheel_evidence.expected_package,
    )
