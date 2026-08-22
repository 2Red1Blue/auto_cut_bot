from __future__ import annotations

import os
import subprocess
import sys
import venv
import zipfile
from importlib import import_module
from pathlib import Path

from tools.architecture.import_firewall import assert_clean

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KERNEL_ROOT = REPOSITORY_ROOT / "packages" / "autocut-kernel"
KERNEL_SOURCE = KERNEL_ROOT / "src" / "autocut_kernel"
AGENT_RUNTIME = REPOSITORY_ROOT / "auto_cut_bot" / "autocut_agent_runtime"


def test_kernel_identity_requires_explicit_name_and_version(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_ROOT / "src"))
    kernel_identity_type = import_module("autocut_kernel").KernelIdentity
    compose_runtime = import_module("auto_cut_bot.autocut_agent_runtime").compose_runtime
    identity = kernel_identity_type(name="autocut", version="0.1.0")

    assert identity.name == "autocut"
    assert identity.version == "0.1.0"
    assert compose_runtime(identity).kernel is identity


def test_kernel_and_agent_runtime_stay_on_one_way_zero_legacy_boundary() -> None:
    assert_clean(
        [KERNEL_SOURCE, AGENT_RUNTIME],
        forbidden_roots={
            "ac_auto_cut",
            "artifact_bus",
            "artifactbus",
            "autocut_core",
            "autocut_pipeline_runtime",
            "auto_cut_bot",
            "nanobot",
        },
    )


def test_repository_has_exactly_one_kernel_source_tree() -> None:
    sources = sorted(
        path.relative_to(REPOSITORY_ROOT)
        for path in REPOSITORY_ROOT.rglob("autocut_kernel")
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and "build" not in path.relative_to(REPOSITORY_ROOT).parts
    )

    assert sources == [Path("packages/autocut-kernel/src/autocut_kernel")]


def test_kernel_wheel_installs_without_checkout_or_legacy_distribution(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheelhouse"
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            str(KERNEL_ROOT),
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_directory.glob("autocut_kernel-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "Requires-Dist:" not in metadata
    assert "autocut_core" not in metadata.lower()

    environment = tmp_path / "clean-environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": ""}
    completed = subprocess.run(
        [str(python), "-c", "from autocut_kernel import KernelIdentity; print(KernelIdentity('k', '1'))"],
        check=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert "KernelIdentity" in completed.stdout
