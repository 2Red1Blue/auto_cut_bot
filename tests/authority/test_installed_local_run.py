"""Synthetic content transport tests; none produces installed production authority."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.registry import installed_local_run as installed
from autocut_kernel.registry.authority_profiles import decode_runtime_calibration_policy_source
from autocut_kernel.registry.installed_local_run import (
    LocalRunResourceError,
    compute_local_profile_registry_sha256,
    decode_local_run_resource,
    load_installed_local_run_resource,
)

from tests.authority.test_authority_profile_sources import (
    REPO_ROOT,
    _hash,
    _narrative_mapping,
    _raw,
    _run_mapping,
    _shadow_mapping,
)


def _encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _rehash_chain(chain, kind: str) -> None:
    chain["registry_set_sha256"] = compute_local_profile_registry_sha256(
        profile_kind=kind, narrative_raw=base64.b64decode(chain["narrative_raw_base64"]),
        profile_raw=base64.b64decode(chain["profile_raw_base64"]),
        schema_raw=base64.b64decode(chain["schema_raw_base64"]),
    )


def _resource_mapping():
    narrative = _narrative_mapping()
    shadow = _shadow_mapping(narrative)
    local_run = _run_mapping(narrative, shadow)
    schemas = REPO_ROOT / "governance/schemas"
    old = {
        "authority_lock_sha256": _hash("synthetic-old-lock"),
        "narrative_raw_base64": _encoded(_raw(narrative)),
        "profile_raw_base64": _encoded(_raw(shadow)),
        "schema_raw_base64": _encoded((schemas / "shadow-calibration-profile.schema.json").read_bytes()),
    }
    _rehash_chain(old, "shadow_calibration_v1")
    local_run["predecessor_shadow_profile"]["registry_set_sha256"] = old["registry_set_sha256"]
    local_run["predecessor_shadow_profile"]["authority_lock_sha256"] = old["authority_lock_sha256"]
    current = {
        "authority_lock_sha256": _hash("synthetic-current-lock"),
        "narrative_raw_base64": _encoded(_raw(narrative)),
        "profile_raw_base64": _encoded(_raw(local_run)),
        "schema_raw_base64": _encoded((schemas / "local-run-profile.schema.json").read_bytes()),
    }
    _rehash_chain(current, "local_run_v1")
    return {"schema_version": "installed-local-run-authority-v1", "current": current, "predecessor": old}


def _decode(mapping):
    raw = canonical_json_bytes(mapping)
    return decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))


def test_complete_raw_source_resource_round_trips_without_granting_capability() -> None:
    mapping = _resource_mapping()
    resource = _decode(mapping)
    assert resource.current_registry_sha256 == mapping["current"]["registry_set_sha256"]
    assert resource.predecessor_registry_sha256 == mapping["predecessor"]["registry_set_sha256"]
    assert resource.current_lock_sha256 != resource.predecessor_lock_sha256
    assert resource.current_registry_sha256 != resource.predecessor_registry_sha256
    assert resource.shadow.source_sha256 == sha256_bytes(base64.b64decode(mapping["predecessor"]["profile_raw_base64"]))
    assert resource.local_run.source_sha256 == sha256_bytes(base64.b64decode(mapping["current"]["profile_raw_base64"]))
    assert not any(hasattr(resource, field) for field in ("snapshot", "bootstrap_request", "ready", "accepted"))
    with pytest.raises(FrozenInstanceError):
        resource.current_lock_sha256 = _hash("replace")
    reformatted = json.dumps(mapping, indent=4).encode() + b"\n"
    assert decode_local_run_resource(reformatted, expected_sha256=sha256_bytes(reformatted)) == resource


def test_runtime_calibration_policy_grammar_is_static_and_device_scoped() -> None:
    raw = canonical_json_bytes(
        {
            "schema_version": "autocut-runtime-calibration-policy-v1",
            "profile_source_sha256": _hash("runtime-profile"),
            "registry_snapshot_sha256": _hash("runtime-registry"),
            "capabilities": [
                {"runtime_capability_id": "mac_cpu", "device_class": "cpu"},
                {"runtime_capability_id": "pc_cuda", "device_class": "cuda"},
            ],
        }
    )
    policy = decode_runtime_calibration_policy_source(raw)
    assert [item.runtime_capability_id for item in policy.capabilities] == ["mac_cpu", "pc_cuda"]


def test_registry_identity_has_independent_manual_role_ordered_hash_oracle() -> None:
    raw_sources = (b"narrative", b"profile", b"schema")
    material = {
        "schema_version": "local-profile-registry-v1", "profile_kind": "local_run_v1",
        "sources": [{"role": role, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
                    for role, raw in zip(("narrative", "profile", "profile_schema"), raw_sources, strict=True)],
    }
    expected = "sha256:" + hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    options = dict(profile_kind="local_run_v1", narrative_raw=raw_sources[0], profile_raw=raw_sources[1], schema_raw=raw_sources[2])
    assert compute_local_profile_registry_sha256(**options) == expected
    for changed in ({"profile_kind": "shadow_calibration_v1"}, {"narrative_raw": b"narrative\n"},
                    {"profile_raw": b"other"}, {"schema_raw": b"other"}):
        assert compute_local_profile_registry_sha256(**{**options, **changed}) != expected


@pytest.mark.parametrize("changed", ({"profile_kind": "generic"}, {"profile_kind": True}, {"narrative_raw": b""}, {"profile_raw": "{}"}, {"schema_raw": b" " * (4 * 1024 * 1024 + 1)}))
def test_identity_helper_rejects_invalid_kind_and_source_bounds(changed) -> None:
    options = dict(profile_kind="local_run_v1", narrative_raw=b"{}", profile_raw=b"{}", schema_raw=b"{}")
    with pytest.raises(LocalRunResourceError):
        compute_local_profile_registry_sha256(**{**options, **changed})


@pytest.mark.parametrize("chain", ("current", "predecessor"))
@pytest.mark.parametrize("field", ("registry_set_sha256", "authority_lock_sha256"))
@pytest.mark.parametrize("value", ("sha256:" + "0" * 64, "invalid", True, None))
def test_nonzero_exact_hashes_are_required(chain: str, field: str, value) -> None:
    mapping = _resource_mapping()
    mapping[chain][field] = value
    with pytest.raises(LocalRunResourceError):
        _decode(mapping)


@pytest.mark.parametrize("chain", ("current", "predecessor"))
def test_metadata_cannot_repeat_an_unrelated_generic_registry_hash(chain: str) -> None:
    mapping = _resource_mapping()
    mapping[chain]["registry_set_sha256"] = _hash("generic-registry-is-not-this-source-set")
    with pytest.raises(LocalRunResourceError, match="Registry identity"):
        _decode(mapping)


@pytest.mark.parametrize("field", ("profile_version", "source_sha256", "registry_set_sha256", "authority_lock_sha256"))
def test_local_run_must_name_all_four_exact_predecessor_identities(field: str) -> None:
    mapping = _resource_mapping()
    current = mapping["current"]
    profile = json.loads(base64.b64decode(current["profile_raw_base64"]))
    profile["predecessor_shadow_profile"][field] = "2" if field == "profile_version" else _hash("foreign")
    current["profile_raw_base64"] = _encoded(_raw(profile))
    _rehash_chain(current, "local_run_v1")
    with pytest.raises(LocalRunResourceError):
        _decode(mapping)


@pytest.mark.parametrize("mutation", ("current-schema", "old-schema", "current-narrative", "old-narrative", "whole-schema-as-contract", "component-contract", "schema-ref", "native", "publication"))
def test_sources_rehashing_transport_does_not_bypass_grammar_or_component_binding(mutation: str) -> None:
    mapping = _resource_mapping()
    old, current = mapping["predecessor"], mapping["current"]
    chain = old if mutation.startswith("old-") else current
    if mutation.endswith("schema"):
        chain["schema_raw_base64"] = _encoded(base64.b64decode(chain["schema_raw_base64"]) + b"\n")
    elif mutation.endswith("narrative"):
        chain["narrative_raw_base64"] = _encoded(base64.b64decode(chain["narrative_raw_base64"]) + b"\n")
    else:
        profile = json.loads(base64.b64decode(current["profile_raw_base64"]))
        if mutation in {"whole-schema-as-contract", "component-contract"}:
            profile["timed_speech_registry_entry"]["registry_contract_sha256"] = (
                sha256_bytes(base64.b64decode(current["schema_raw_base64"])) if mutation == "whole-schema-as-contract" else _hash("bad-component")
            )
        elif mutation == "schema-ref":
            schema = json.loads(base64.b64decode(current["schema_raw_base64"]))
            schema["$defs"]["timed_speech_registry_entry"]["$dynamicRef"] = "unsupported"
            schema_raw = _raw(schema)
            current["schema_raw_base64"] = _encoded(schema_raw)
            profile["profile_contract_sha256"] = sha256_bytes(schema_raw)
        elif mutation == "native":
            profile["native_timed_speech"]["service_sha256"] = _hash("different-native")
        else:
            profile["capabilities"]["external_publication"] = True
        current["profile_raw_base64"] = _encoded(_raw(profile))
    _rehash_chain(old, "shadow_calibration_v1")
    _rehash_chain(current, "local_run_v1")
    with pytest.raises(LocalRunResourceError):
        _decode(mapping)


@pytest.mark.parametrize("chain", ("current", "predecessor"))
@pytest.mark.parametrize("field", ("narrative_raw_base64", "profile_raw_base64", "schema_raw_base64"))
@pytest.mark.parametrize("value", ("", " ", "e30", "e30=\n", "e31=", "!!!!", "____", "中文", True))
def test_each_source_requires_strict_canonical_base64(chain: str, field: str, value) -> None:
    mapping = _resource_mapping()
    mapping[chain][field] = value
    with pytest.raises(LocalRunResourceError):
        _decode(mapping)


@pytest.mark.parametrize("location", (None, "current", "predecessor"))
@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_resource_objects_are_closed(location: str | None, mutation: str) -> None:
    mapping = _resource_mapping()
    target = mapping if location is None else mapping[location]
    if mutation == "extra":
        target["ready"] = True
    else:
        target.pop(next(iter(target)))
    with pytest.raises(LocalRunResourceError):
        _decode(mapping)


@pytest.mark.parametrize("raw", (b"", b"\xff", b"[]", b"{\"x\":1.0}", b"{\"x\":NaN}", b"{\"x\":1,\"x\":2}", b" " * (16 * 1024 * 1024 + 1)))
def test_malformed_or_oversized_resource_uses_dedicated_error(raw: bytes) -> None:
    with pytest.raises(LocalRunResourceError):
        decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))


def test_component_size_and_strict_json_are_checked_before_profile_decoding() -> None:
    for raw in (b" " * (4 * 1024 * 1024 + 1), b"\xff", b'{"x":1,"x":2}', b'{"x":1.0}'):
        mapping = _resource_mapping()
        mapping["predecessor"]["schema_raw_base64"] = _encoded(raw)
        with pytest.raises(LocalRunResourceError):
            _decode(mapping)


def test_digest_is_over_exact_resource_bytes_and_errors_do_not_echo_source() -> None:
    raw = canonical_json_bytes(_resource_mapping())
    with pytest.raises(LocalRunResourceError, match="digest mismatch"):
        decode_local_run_resource(raw + b"\n", expected_sha256=sha256_bytes(raw))
    secret = "do-not-echo-this-source-value"
    invalid = ('{"' + secret + '":1,"' + secret + '":2}').encode()
    with pytest.raises(LocalRunResourceError) as caught:
        decode_local_run_resource(invalid, expected_sha256=sha256_bytes(invalid))
    assert secret not in "".join(traceback.format_exception(caught.value))


def _install(monkeypatch: pytest.MonkeyPatch, root: Path, raw: bytes, digest: bytes | None = None) -> None:
    directory = root / "_authority"
    directory.mkdir()
    (directory / "local-run.json").write_bytes(raw)
    (directory / "local-run.sha256").write_bytes(digest if digest is not None else (sha256_bytes(raw) + "\n").encode("ascii"))

    def files(package: str):
        assert package == "autocut_kernel"
        return root

    monkeypatch.setattr(installed.resources, "files", files)


def test_fixed_installed_loader_round_trips_and_accepts_no_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = canonical_json_bytes(_resource_mapping())
    _install(monkeypatch, tmp_path, raw)
    assert load_installed_local_run_resource() == decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))
    with pytest.raises(TypeError):
        load_installed_local_run_resource(path=tmp_path)


@pytest.mark.parametrize("digest", (b"", b"sha256:" + b"1" * 64, b"sha256:" + b"1" * 64 + b"\r\n", b"sha256:" + b"A" * 64 + b"\n", b"sha256:" + b"0" * 64 + b"\n", b"\xff" * 71 + b"\n", b"sha256:" + b"1" * 64 + b"\nextra"))
def test_fixed_loader_rejects_digest_framing_and_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: bytes) -> None:
    _install(monkeypatch, tmp_path, canonical_json_bytes(_resource_mapping()), digest)
    with pytest.raises(LocalRunResourceError):
        load_installed_local_run_resource()


@pytest.mark.parametrize("missing", ("local-run.json", "local-run.sha256"))
def test_fixed_loader_rejects_absent_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _install(monkeypatch, tmp_path, canonical_json_bytes(_resource_mapping()))
    (tmp_path / "_authority" / missing).unlink()
    with pytest.raises(LocalRunResourceError, match="unavailable"):
        load_installed_local_run_resource()


def test_installed_reads_are_bounded_and_streams_close(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b" " * (16 * 1024 * 1024 + 1)
    files = {"local-run.json": raw, "local-run.sha256": (sha256_bytes(raw) + "\n").encode()}
    reads = []
    streams = []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size > 0
            return super().read(size)

    class Resource:
        def __init__(self, name=""):
            self.name = name

        def joinpath(self, name):
            return Resource(name)

        def open(self, mode):
            assert mode == "rb"
            stream = Stream(files[self.name])
            streams.append(stream)
            return stream

    monkeypatch.setattr(installed.resources, "files", lambda _package: Resource())
    with pytest.raises(LocalRunResourceError, match="bound"):
        load_installed_local_run_resource()
    assert reads == [73, 16 * 1024 * 1024 + 1]
    assert all(stream.closed for stream in streams)
