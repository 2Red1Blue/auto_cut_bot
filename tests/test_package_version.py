from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name

from auto_cut_bot.project_identity import (
    DISTRIBUTION_NAME,
    PROJECT_REPOSITORY_URL,
    PYPI_JSON_URL,
    PYPI_PROJECT_URL,
)


def test_project_metadata_uses_runtime_distribution_identity() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert canonicalize_name(project["name"]) == canonicalize_name(DISTRIBUTION_NAME)
    assert project["urls"]["Repository"] == PROJECT_REPOSITORY_URL
    assert DISTRIBUTION_NAME == "auto-cut-bot-ai"
    assert PROJECT_REPOSITORY_URL == "https://github.com/2Red1Blue/auto_cut_bot"
    assert PYPI_JSON_URL == "https://pypi.org/pypi/auto-cut-bot-ai/json"
    assert PYPI_PROJECT_URL == "https://pypi.org/project/auto-cut-bot-ai/"


def test_source_checkout_import_uses_pyproject_version_without_metadata() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    script = textwrap.dedent(
        f"""
        import sys
        import types

        sys.path.insert(0, {str(repo_root)!r})
        fake = types.ModuleType("auto_cut_bot.auto_cut_bot")
        fake.Nanobot = object
        fake.RunResult = object
        sys.modules["auto_cut_bot.auto_cut_bot"] = fake

        import auto_cut_bot

        print(auto_cut_bot.__version__)
        """
    )

    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected
