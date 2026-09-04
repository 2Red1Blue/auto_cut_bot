from __future__ import annotations

import base64
import inspect
import json
import os
import shutil
import subprocess
import venv
import zipfile
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.registry import installed_production_qc as installed
from autocut_kernel.rendering.production_qc_collector_capability import (
    ProductionQcCollectorPolicySource,
)

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "packages/autocut-kernel/src/autocut_kernel/registry/_production_qc"


def _raw() -> bytes:
    return (RESOURCES / "production-qc.json").read_bytes()


def test_fixed_loader_returns_static_source_with_explicit_provenance() -> None:
    resource = installed.load_installed_production_qc_resource()
    assert type(resource.policy) is ProductionQcCollectorPolicySource
    assert resource.provenance.authority_revision == 9
    assert resource.policy.profile_id == "production-av-qc-v1"
    assert resource.policy.policy_source_sha256 == sha256_bytes(
        (ROOT / "governance/production-qc-collector-policy.json").read_bytes()
    )
    assert resource.resource_sha256 == sha256_bytes(_raw())
    assert resource.capability_set_profile_sha256 == sha256_bytes(
        (ROOT / "governance/production-qc-collector-capability-set-profile.json").read_bytes()
    )
    assert not inspect.signature(installed.load_installed_production_qc_resource).parameters


def test_packaged_sources_match_the_immutable_locked_git_blobs() -> None:
    from tools.authority.common import git_bytes, load_mapping_bytes
    from tools.authority.lock import validate_authority_lock
    from tools.authority.locked_registry import read_locked_blob

    resource = installed.load_installed_production_qc_resource()
    roots = {"auto_cut_bot": ROOT}
    # Full cross-repository A/B/C validation belongs to the resource build.
    # Here compare every shipped source against that exact immutable C lock;
    # standalone unit tests need no unrelated sibling checkout or database.
    lock_raw = git_bytes(ROOT, resource.provenance.lock_commit, "governance/authority-lock.yaml")
    lock = load_mapping_bytes(lock_raw, where="QC package provenance")
    validate_authority_lock(lock)
    assert resource.provenance.authority_bundle_sha256 == lock["bundle_hash"]
    assert resource.provenance.source_commit == lock["seed_source_commit"]
    assert resource.provenance.inventory_commit == lock["inventory"]["manifest_commit"]
    document = json.loads(_raw())
    for role, name in (
        ("policy", "policy"), ("registry_snapshot", "registry-snapshot"),
        ("capability_set_profile", "capability-set-profile"),
    ):
        for schema in (False, True):
            path = (
                f"governance/schemas/production-qc-collector-{name}.schema.json"
                if schema else f"governance/production-qc-collector-{name}.json"
            )
            raw = read_locked_blob(
                lock=lock, repository_roots=roots, repository="auto_cut_bot", path=path,
                expected_class="schema_source" if schema else "registry_source",
            )
            encoded = document["sources"][role + ("_schema" if schema else "")]
            assert base64.b64decode(encoded, validate=True) == raw


@pytest.mark.parametrize("change", ["provenance", "extra_source", "swapped_roles", "bad_base64", "source_drift", "duplicate_key"])
def test_rehashed_substitution_is_rejected(change: str) -> None:
    document = json.loads(_raw())
    if change == "provenance":
        document["provenance"]["lock_commit"] = "1" * 40
    elif change == "extra_source":
        document["sources"]["caller_path"] = "/tmp/policy.json"
    elif change == "swapped_roles":
        document["sources"]["policy"] = document["sources"]["registry_snapshot"]
    elif change == "bad_base64":
        document["sources"]["policy"] += "\n"
    elif change == "source_drift":
        policy = json.loads(base64.b64decode(document["sources"]["policy"]))
        policy["profile_id"] = "caller-selected-profile"
        document["sources"]["policy"] = base64.b64encode(canonical_json_bytes(policy)).decode()
    raw = canonical_json_bytes(document)
    if change == "duplicate_key":
        raw = b'{"schema_version":"installed-production-qc-policy-v1",' + raw[1:]
    with pytest.raises(installed.InstalledProductionQcResourceError):
        installed.decode_production_qc_resource(raw, expected_sha256=sha256_bytes(raw))


@pytest.mark.parametrize("raw", [b"", b"x" * (128 * 1024 + 1), b"{", b'"string"'])
def test_invalid_or_oversized_resource_rejected(raw: bytes) -> None:
    with pytest.raises(installed.InstalledProductionQcResourceError):
        installed.decode_production_qc_resource(raw, expected_sha256=sha256_bytes(raw))


def test_resource_reformat_and_digest_mismatch_are_rejected() -> None:
    raw = _raw()
    with pytest.raises(installed.InstalledProductionQcResourceError, match="digest mismatch"):
        installed.decode_production_qc_resource(raw, expected_sha256="sha256:" + "0" * 64)
    with pytest.raises(installed.InstalledProductionQcResourceError, match="not canonical"):
        installed.decode_production_qc_resource(raw + b"\n", expected_sha256=sha256_bytes(raw + b"\n"))


@pytest.mark.parametrize("framing", [b"", b"0" * 72, b"sha256:" + b"a" * 64, b"sha256:" + b"a" * 64 + b"\r\n"])
def test_fixed_loader_rejects_bad_sibling_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framing: bytes
) -> None:
    root = tmp_path / "_production_qc"
    root.mkdir()
    (root / "production-qc.sha256").write_bytes(framing)
    monkeypatch.setattr(installed.resources, "files", lambda package: tmp_path)
    with pytest.raises(installed.InstalledProductionQcResourceError, match="framing|digest"):
        installed.load_installed_production_qc_resource()


def test_fixed_loader_has_no_path_override_or_missing_resource_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installed.resources, "files", lambda package: tmp_path)
    with pytest.raises(TypeError):
        installed.load_installed_production_qc_resource(path=RESOURCES)  # type: ignore[call-arg]
    with pytest.raises(installed.InstalledProductionQcResourceError, match="unavailable"):
        installed.load_installed_production_qc_resource()


def test_standalone_wheel_loads_qc_resource_without_checkout(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    source = tmp_path / "build-source"
    shutil.copytree(
        ROOT / "packages/autocut-kernel", source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "*.egg-info"),
    )
    wheelhouse = tmp_path / "wheels"
    subprocess.run(
        [uv, "build", "--offline", "--wheel", "--out-dir", str(wheelhouse), str(source)],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    wheel, = wheelhouse.glob("autocut_kernel-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        for name in ("production-qc.json", "production-qc.sha256"):
            assert archive.read(f"autocut_kernel/registry/_production_qc/{name}") == (RESOURCES / name).read_bytes()
    environment = tmp_path / "installed"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [uv, "pip", "install", "--offline", "--python", str(python), "--no-deps", str(wheel)],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        [str(python), "-I", "-c",
         "from autocut_kernel.registry.installed_production_qc import load_installed_production_qc_resource; "
         "import sys; r=load_installed_production_qc_resource(); "
         "assert not any(n.startswith('tools.') for n in sys.modules); "
         "print(r.policy.profile_id, r.provenance.authority_revision)"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "production-av-qc-v1 9"
