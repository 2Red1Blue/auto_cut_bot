from __future__ import annotations

import os
import shutil
import subprocess
import venv
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_source_tests_import_kernel_from_current_repository() -> None:
    import autocut_kernel

    kernel_file = Path(autocut_kernel.__file__).resolve()
    expected_root = (REPOSITORY_ROOT / "packages/autocut-kernel/src").resolve()
    assert kernel_file.is_relative_to(expected_root)


def test_root_wheel_installs_pipeline_server_with_postgres_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(wheelhouse),
            str(REPOSITORY_ROOT),
        ],
        check=True,
        cwd=tmp_path,
        env={**os.environ, "AUTO_CUT_BOT_SKIP_WEBUI_BUILD": "1"},
        capture_output=True,
        text=True,
    )
    wheel = next(wheelhouse.glob("auto_cut_bot_ai-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = frozenset(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    # The root package deliberately requests psycopg's binary extra so a clean
    # wheel install includes the libpq driver needed by the pipeline server.
    # Hatch normalizes the requirement ordering in wheel metadata.
    assert "Name: auto-cut-bot-ai" in metadata
    assert "Project-URL: Repository, https://github.com/2Red1Blue/auto_cut_bot" in metadata
    assert "Requires-Dist: psycopg[binary]<4.0.0,>=3.0.0" in metadata
    assert "autocut_kernel/__init__.py" in names

    environment = tmp_path / "clean-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            f"{wheel}[api]",
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from autocut_kernel.store import PostgresRuntimeStore; "
                "from auto_cut_bot.pipeline.runtime.composition import "
                "compose_pipeline_runtime_from_environment; "
                "print(PostgresRuntimeStore.__name__, "
                "compose_pipeline_runtime_from_environment.__name__)"
            ),
        ],
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == (
        "PostgresRuntimeStore compose_pipeline_runtime_from_environment"
    )
