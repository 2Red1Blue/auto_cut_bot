"""Both wheel configurations carry a deliberately prepared local authority resource."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from authority.local_run_packaging import prepare_locked_local_run_package  # noqa: E402

from tests.authority.test_local_run_calibration import FakeAcceptedAnchorReader  # noqa: E402
from tests.authority.test_local_run_resource import _synthetic_accepted_sources  # noqa: E402

KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src" / "autocut_kernel"
_RESOURCE_NAMES = frozenset({
    "autocut_kernel/_authority/local-run.json",
    "autocut_kernel/_authority/local-run.sha256",
})


def _copy_kernel_source(destination: Path) -> Path:
    shutil.copytree(
        KERNEL_SOURCE,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_authority"),
    )
    return destination


def _root_build_source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root-build-source"
    root.mkdir(parents=True)
    for name in ("pyproject.toml", "hatch_build.py", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(REPOSITORY_ROOT / name, root / name)
    (root / "auto_cut_bot").mkdir()
    (root / "auto_cut_bot" / "__init__.py").write_text('"""Minimal wheel fixture package."""\n')
    kernel = _copy_kernel_source(root / "packages" / "autocut-kernel" / "src" / "autocut_kernel")
    return root, kernel


def _standalone_build_source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "standalone-kernel-build-source"
    root.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "packages" / "autocut-kernel" / "pyproject.toml", root / "pyproject.toml")
    return root, _copy_kernel_source(root / "src" / "autocut_kernel")


def _build_and_load(*, source: Path, expected_prefix: str, tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    tmp_path.mkdir()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    subprocess.run(
        [uv, "build", "--offline", "--wheel", "--out-dir", str(wheelhouse), str(source)],
        check=True,
        cwd=tmp_path,
        env={**os.environ, "AUTO_CUT_BOT_SKIP_WEBUI_BUILD": "1"},
        capture_output=True,
        text=True,
    )
    wheel = next(wheelhouse.glob(f"{expected_prefix}-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert _RESOURCE_NAMES <= frozenset(archive.namelist())
    environment = tmp_path / "clean-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [uv, "pip", "install", "--offline", "--python", str(python), "--no-deps", str(wheel)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    runtime = tmp_path / "no-checkout-runtime"
    runtime.mkdir()
    completed = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from autocut_kernel.registry.installed_local_run import "
                "load_installed_local_run_resource; "
                "print(load_installed_local_run_resource().local_run.profile_version)"
            ),
        ],
        check=True,
        cwd=runtime,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "1"


def test_root_and_standalone_wheels_load_prepared_resource_without_checkout_or_tools(tmp_path: Path) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    sources, anchor = _synthetic_accepted_sources(authority_root)
    for name, builder, prefix in (
        ("root", _root_build_source, "auto_cut_bot"),
        ("standalone", _standalone_build_source, "autocut_kernel"),
    ):
        build_root, kernel_package = builder(tmp_path / name)
        prepare_locked_local_run_package(
            **sources.options,
            store=FakeAcceptedAnchorReader(anchor),
            destination_kernel_package=kernel_package,
        )
        _build_and_load(source=build_root, expected_prefix=prefix, tmp_path=tmp_path / f"{name}-wheel")
