from __future__ import annotations

import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_root_wheel_installs_pipeline_server_with_postgres_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(REPOSITORY_ROOT),
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    wheel = next(wheelhouse.glob("auto_cut_bot-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "Requires-Dist: psycopg<4.0.0,>=3.0.0" in metadata

    environment = tmp_path / "clean-environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
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
            "from auto_cut_bot.api.server import create_app; print(create_app.__name__)",
        ],
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "create_app"
