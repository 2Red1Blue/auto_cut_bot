from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from authority.cli import main
from authority.common import sha256_bytes
from authority.errors import GateViolation
from authority.source_candidate_gate import verify_pre_a_source_candidate


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _pre_a_repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    predecessor = _commit(root, "seed")
    fixture_path = "tests/authority/fixtures/synthetic-sensitive/literal.txt"
    fixture = root / fixture_path
    fixture.parent.mkdir(parents=True)
    raw = b"AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1\nsk-" + b"preafixure00000000000000000000\n"
    fixture.write_bytes(raw)
    manifest_path = "governance/synthetic-sensitive-fixtures.manifest.yaml"
    manifest_file = root / manifest_path
    manifest_file.parent.mkdir(parents=True)
    manifest_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "allowed_root": "tests/authority/fixtures/synthetic-sensitive",
                "marker": "AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1",
                "fixtures": [
                    {
                        "fixture_id": "AUTH-SYNTHETIC-PRE-A-001",
                        "path": fixture_path,
                        "sha256": sha256_bytes(raw),
                        "profile": "test_fixture",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source = root / "tools/authority/gate.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "governance", "tools/authority", "tests/authority")
    return root, predecessor, manifest_path


def test_pre_a_source_candidate_allows_exact_synthetic_fixture_and_cli(tmp_path: Path) -> None:
    root, predecessor, manifest_path = _pre_a_repository(tmp_path)
    receipt = verify_pre_a_source_candidate(
        root=root,
        predecessor_commit=predecessor,
        synthetic_fixture_manifest_path=manifest_path,
    )
    assert receipt["decision"] == "allow"
    assert (
        main(
            [
                "verify-source-candidate",
                "--repository-root",
                str(root),
                "--predecessor-commit",
                predecessor,
                "--synthetic-fixture-manifest",
                manifest_path,
            ]
        )
        == 0
    )


def test_pre_a_rejects_inventory_mixed_into_source_commit(tmp_path: Path) -> None:
    root, predecessor, manifest_path = _pre_a_repository(tmp_path)
    (root / "governance/authority-sources.yaml").write_text("schema_version: 1.0.0\n")
    _git(root, "add", "governance/authority-sources.yaml")
    with pytest.raises(GateViolation, match="AUTH-PRE-A-PHASE-MIX"):
        verify_pre_a_source_candidate(
            root=root,
            predecessor_commit=predecessor,
            synthetic_fixture_manifest_path=manifest_path,
        )
