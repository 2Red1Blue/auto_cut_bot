from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash, sha256_bytes
from autocut_kernel.contracts.compiler.errors import RegistryValidationError
from autocut_kernel.contracts.compiler.registry_source import load_registry_source_manifest


def _write_valid_source(root: Path) -> Path:
    paths: list[dict[str, object]] = []
    for order, kind in enumerate(("common", "commands", "stage_01", "stage_02", "stage_03", "stage_04", "stage_05", "publication")):
        path = f"{kind}/example.json"
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f'{{"pack":"{kind}"}}\n'.encode())
        file_hash = sha256_bytes(target.read_bytes())
        paths.append({"pack_id": f"{kind}-pack", "pack_order": order, "kind": kind, "root": kind,
                      "source_paths": [{"path": path, "file_hash": file_hash}],
                      "source_tree_hash": canonical_json_hash([{"path": path, "file_hash": file_hash}])})
    docs = []
    for kind in ("artifacts", "commands", "rules", "strategies", "traces"):
        body = {"format": "autocut.registry.source/v1", "registry_kind": kind, "contract_version": "2.1.3",
                "registry_version": "1.0.0", "pack_id": "source-pack", "entries": [{"id": kind}]}
        body["document_hash"] = canonical_json_hash(body)
        target = root / "common" / "registries" / f"{kind}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"{key}: {value}" if not isinstance(value, list) else f"{key}:\n  - id: {kind}" for key, value in body.items()) + "\n")
        docs.append({"registry_kind": kind, "path": f"common/registries/{kind}.yaml", "document_hash": body["document_hash"]})
    manifest = {"format": "autocut.registry-set.source/v1", "contract_version": "2.1.3", "registry_set_version": "1.0.0",
                "pack_id": "source-pack", "source_packs": paths, "registry_documents": docs}
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    target = root / "common" / "registry_set.yaml"
    target.write_text(_yaml(manifest))
    return root


def _yaml(value: object, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)) and child:
                lines.append(f"{prefix}{key}:")
                lines.append(_yaml(child, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_scalar(child)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for child in value:
            if isinstance(child, dict) and child:
                first, *rest = child.items()
                key, nested = first
                lines.append(f"{prefix}- {key}: {_scalar(nested)}")
                for extra_key, extra_value in rest:
                    if isinstance(extra_value, (dict, list)) and extra_value:
                        lines.append(f"{prefix}  {extra_key}:")
                        lines.append(_yaml(extra_value, indent + 4))
                    else:
                        lines.append(f"{prefix}  {extra_key}: {_scalar(extra_value)}")
            else:
                lines.append(f"{prefix}- {_scalar(child)}")
        return "\n".join(lines)
    return f"{prefix}{_scalar(value)}"


def _scalar(value: object) -> str:
    if value == []:
        return "[]"
    if value == {}:
        return "{}"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _resign_command_source_locator(
    root: Path, *, declared_path: str, physical_path: str | None
) -> None:
    """Reissue every manifest signature after replacing the command locator.

    ``physical_path`` deliberately lets an alias resolve to a real, distinct
    raw file.  That keeps the negative tests from being only syntax probes:
    apart from the locator grammar itself, their entry, tree, and manifest are
    all correctly hash-bound.
    """
    from autocut_kernel.contracts.compiler.registry_source import _load_yaml

    manifest_path = root / "common" / "registry_set.yaml"
    manifest = _load_yaml(manifest_path)
    packs = manifest["source_packs"]
    assert isinstance(packs, list)
    commands = packs[1]
    assert isinstance(commands, dict) and commands["kind"] == "commands"
    source_paths = commands["source_paths"]
    assert isinstance(source_paths, list) and len(source_paths) == 1
    source = source_paths[0]
    assert isinstance(source, dict)

    if physical_path is None:
        target = root / "commands" / "example.json"
    else:
        target = root / physical_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"signed":"locator-test"}\n')
    source["path"] = declared_path
    source["file_hash"] = sha256_bytes(target.read_bytes())
    commands["source_tree_hash"] = canonical_json_hash(
        sorted(source_paths, key=lambda item: item["path"].encode("utf-8"))
    )
    manifest.pop("registry_set_hash")
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    manifest_path.write_text(_yaml(manifest) + "\n")

    # Demonstrate that the rejection below is not due to stale signatures.
    reissued = _load_yaml(manifest_path)
    assert reissued["registry_set_hash"] == canonical_json_hash(
        {key: value for key, value in reissued.items() if key != "registry_set_hash"}
    )
    reissued_commands = reissued["source_packs"][1]
    assert reissued_commands["source_tree_hash"] == canonical_json_hash(
        sorted(reissued_commands["source_paths"], key=lambda item: item["path"].encode("utf-8"))
    )


def test_loads_a_minimal_complete_eight_pack_manifest(tmp_path: Path) -> None:
    manifest = load_registry_source_manifest(_write_valid_source(tmp_path))
    assert len(manifest.source_packs) == 8
    assert [document.registry_kind for document in manifest.registry_documents] == sorted(
        ("artifacts", "commands", "rules", "strategies", "traces")
    )


def test_rejects_signed_duplicate_source_pack_id(tmp_path: Path) -> None:
    """Pack identities must be unique even when the signed manifest is reissued."""
    from autocut_kernel.contracts.compiler.registry_source import _load_yaml

    root = _write_valid_source(tmp_path)
    path = root / "common" / "registry_set.yaml"
    manifest = _load_yaml(path)
    packs = manifest["source_packs"]
    assert isinstance(packs, list)
    assert isinstance(packs[0], dict) and isinstance(packs[1], dict)
    packs[1]["pack_id"] = packs[0]["pack_id"]
    manifest.pop("registry_set_hash")
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    path.write_text(_yaml(manifest) + "\n")

    with pytest.raises(RegistryValidationError, match="pack_id is duplicated"):
        load_registry_source_manifest(root)


@pytest.mark.parametrize("replacement", [
    "format: autocut.registry-set.source/v1\nformat: other\n",
    "format: &value autocut.registry-set.source/v1\n",
    "format: !!str autocut.registry-set.source/v1\n",
])
def test_rejects_ambiguous_yaml_syntax(tmp_path: Path, replacement: str) -> None:
    root = _write_valid_source(tmp_path)
    path = root / "common" / "registry_set.yaml"
    path.write_text(replacement + path.read_text().split("\n", 1)[1])
    with pytest.raises(RegistryValidationError):
        load_registry_source_manifest(root)


def test_rejects_tree_hash_mismatch(tmp_path: Path) -> None:
    root = _write_valid_source(tmp_path)
    (root / "commands" / "example.json").write_text('{"changed":true}\n')
    with pytest.raises(RegistryValidationError, match="file_hash"):
        load_registry_source_manifest(root)


def test_rejects_registry_document_semantic_hash_mismatch(tmp_path: Path) -> None:
    root = _write_valid_source(tmp_path)
    path = root / "common" / "registries" / "rules.yaml"
    path.write_text(path.read_text().replace("registry_version: 1.0.0", "registry_version: 1.0.1"))
    with pytest.raises(RegistryValidationError, match="document_hash"):
        load_registry_source_manifest(root)


def test_rejects_source_path_outside_its_pack(tmp_path: Path) -> None:
    root = _write_valid_source(tmp_path)
    path = root / "common" / "registry_set.yaml"
    path.write_text(path.read_text().replace('path: "commands/example.json"', 'path: "common/example.json"'))
    with pytest.raises(RegistryValidationError, match="contained"):
        load_registry_source_manifest(root)


@pytest.mark.parametrize(
    ("unsafe_path", "physical_path"),
    [
        ("commands/\u0001example.json", "commands/\u0001example.json"),
        ("commands/\u001fexample.json", "commands/\u001fexample.json"),
        ("commands/\u007fexample.json", "commands/\u007fexample.json"),
        ("commands\\example.json", "commands\\example.json"),
        ("commands//example.json", "commands/example.json"),
        ("commands/./example.json", "commands/example.json"),
        ("commands/../common/alias-target.json", "common/alias-target.json"),
        ("commands/例.json", "commands/例.json"),
        ("commands/\u202eexample.json", "commands/\u202eexample.json"),
        ("commands/space name.json", "commands/space name.json"),
        ("commands/percent%20name.json", "commands/percent%20name.json"),
        ("commands/hash#name.json", "commands/hash#name.json"),
        ("commands/query?name.json", "commands/query?name.json"),
        ("commands/colon:name.json", "commands/colon:name.json"),
        ("commands/at@name.json", "commands/at@name.json"),
    ],
)
def test_rejects_fully_signed_noncanonical_source_path_spellings(
    tmp_path: Path, unsafe_path: str, physical_path: str
) -> None:
    """Reject an unsafe spelling even when it names an actual signed raw file."""
    root = _write_valid_source(tmp_path)
    _resign_command_source_locator(
        root, declared_path=unsafe_path, physical_path=physical_path
    )

    with pytest.raises(RegistryValidationError, match="canonical ASCII|unsafe path"):
        load_registry_source_manifest(root)


@pytest.mark.parametrize(
    "unsafe_path",
    ["\u0000", "/commands/example.json", "commands/example.json/"],
)
def test_rejects_signed_unrepresentable_source_path_spellings(
    tmp_path: Path, unsafe_path: str
) -> None:
    """Some forbidden spellings cannot denote a portable regular file at all."""
    root = _write_valid_source(tmp_path)
    _resign_command_source_locator(root, declared_path=unsafe_path, physical_path=None)

    with pytest.raises(RegistryValidationError, match="canonical ASCII|unsafe path"):
        load_registry_source_manifest(root)


def test_rejects_fully_signed_symlinked_source(tmp_path: Path) -> None:
    """A valid manifest cannot turn a final symlink into source authority."""
    root = _write_valid_source(tmp_path)
    target = root / "commands" / "example.json"
    replacement = root / "commands" / "linked.json"
    replacement.symlink_to(target)
    _resign_command_source_locator(
        root, declared_path="commands/linked.json", physical_path=None
    )

    with pytest.raises(RegistryValidationError, match="symbolic link"):
        load_registry_source_manifest(root)


def test_rejects_signed_source_beneath_symlinked_directory(tmp_path: Path) -> None:
    """Every intermediate component, not only the final file, is no-follow."""
    root = _write_valid_source(tmp_path / "source")
    detached = tmp_path / "detached-commands"
    (root / "commands").rename(detached)
    (root / "commands").symlink_to(detached, target_is_directory=True)

    with pytest.raises(RegistryValidationError, match="symbolic link"):
        load_registry_source_manifest(root)


def test_rejects_signed_fifo_source_without_blocking_or_leaking_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed source locator may never block the compiler on a FIFO."""
    from autocut_kernel.contracts.compiler import registry_source

    root = _write_valid_source(tmp_path)
    source_path = root / "commands" / "example.json"
    source_path.unlink()
    os.mkfifo(source_path)

    real_open = os.open
    real_close = os.close
    fifo_fds: list[int] = []
    closed_fds: list[int] = []

    def tracking_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            fifo_fds.append(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed_fds.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(registry_source.os, "open", tracking_open)
    monkeypatch.setattr(
        registry_source.os,
        "supports_dir_fd",
        frozenset({*os.supports_dir_fd, tracking_open}),
    )
    monkeypatch.setattr(registry_source.os, "close", tracking_close)

    with pytest.raises(RegistryValidationError, match="regular file"):
        load_registry_source_manifest(root)

    assert len(fifo_fds) == 1
    assert fifo_fds[0] in closed_fds


def test_rejects_symlinked_source_root(tmp_path: Path) -> None:
    real_root = _write_valid_source(tmp_path / "real-source")
    alias = tmp_path / "source-alias"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(RegistryValidationError, match="source root"):
        load_registry_source_manifest(alias)


def test_fixed_root_descriptor_survives_deterministic_root_path_swap(tmp_path: Path) -> None:
    """Once opened, replacing the pathname cannot redirect the source reader."""
    from autocut_kernel.contracts.compiler.registry_source import _FixedRootReader

    root = tmp_path / "source"
    trusted = root / "common" / "value.txt"
    trusted.parent.mkdir(parents=True)
    trusted.write_bytes(b"trusted-root")

    with _FixedRootReader(root) as reader:
        detached = tmp_path / "detached-source"
        root.rename(detached)
        replacement = root / "common" / "value.txt"
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b"replacement-root")
        assert reader.read_bytes("common/value.txt", label="root swap") == b"trusted-root"


def test_descriptor_relative_walk_survives_deterministic_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent fd remains authoritative if its directory name is replaced."""
    import os

    from autocut_kernel.contracts.compiler import registry_source

    root = tmp_path / "source"
    trusted = root / "commands" / "example.json"
    trusted.parent.mkdir(parents=True)
    trusted.write_bytes(b"trusted-directory")
    real_open = os.open
    swapped = False

    with registry_source._FixedRootReader(root) as reader:

        def swapping_open(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == "example.json" and dir_fd is not None and not swapped:
                swapped = True
                detached = root / "detached-commands"
                (root / "commands").rename(detached)
                replacement = root / "commands" / "example.json"
                replacement.parent.mkdir()
                replacement.write_bytes(b"replacement-directory")
            return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

        monkeypatch.setattr(registry_source.os, "open", swapping_open)
        assert (
            reader.read_bytes("commands/example.json", label="directory swap")
            == b"trusted-directory"
        )
        assert swapped


def test_forbidden_yaml_token_scanner_keeps_escaped_json_quotes_inside_strings() -> None:
    from autocut_kernel.contracts.compiler.registry_source import _has_forbidden_yaml_token

    assert _has_forbidden_yaml_token('field: "a\\\" # still a JSON string"') is False
    assert _has_forbidden_yaml_token('field: "value" # YAML comment') is True


def _write_closed_source(root: Path) -> Path:
    """Create a small but genuinely closed eight-pack authority snapshot."""
    from autocut_kernel.contracts.compiler.registry_closure import _MATRIX
    from autocut_kernel.contracts.compiler.registry_entries import CLOSED_CAPABILITIES

    def write(path: str, value: bytes) -> str:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        return sha256_bytes(value)

    source_paths: dict[str, list[dict[str, str]]] = {}
    profile_schema = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "x-autocut-role-declarations": {
                "input": [], "policy": [], "lifecycle_slots": []
            },
            "x-autocut-state-machine": {
                "states": ["state:reserved", "state:succeeded"],
                "transitions": [{
                    "transition_id": "transition:complete",
                    "from_state": "state:reserved",
                    "to_state": "state:succeeded",
                }],
            },
            "x-autocut-transition": {
                "transition_id": "transition:complete",
                "from_state": "state:reserved",
                "to_state": "state:succeeded",
            },
        }, separators=(",", ":")
    ).encode() + b"\n"
    for kind in ("common", "commands", "stage_01", "stage_02", "stage_03", "stage_04", "stage_05", "publication"):
        files = [(f"{kind}/entry.json", b'{"entry":true}\n'), (f"{kind}/schema.json", profile_schema), (f"{kind}/impl.md", b"implementation\n"), (f"{kind}/test.json", b"{}\n")]
        source_paths[kind] = [{"path": path, "file_hash": write(path, body)} for path, body in files]

    def h(path: str) -> str:
        return next(item["file_hash"] for values in source_paths.values() for item in values if item["path"] == path)

    common = "common"
    ownership = {
        "owner_pack": "common", "owner_source_path": f"{common}/entry.json",
        "owner_source_hash": h(f"{common}/entry.json"), "owner_contract_path": f"{common}/schema.json",
        "owner_contract_hash": canonical_json_hash(json.loads((root / f"{common}/schema.json").read_text())),
    }
    schema_hash = ownership["owner_contract_hash"]
    raw_hash = h(f"{common}/impl.md")
    strategy_base = {
        "component_version": "2.1.3", "implementation_contract_path": f"{common}/impl.md",
        "implementation_contract_hash": raw_hash, "input_schema_path": f"{common}/schema.json",
        "input_schema_hash": schema_hash, "output_schema_path": f"{common}/schema.json",
        "output_schema_hash": schema_hash, "determinism": "deterministic", "capabilities": sorted(CLOSED_CAPABILITIES), **ownership,
    }
    strategies = [
        {"component_id": "handler", "kind": "command_handler", **strategy_base},
        {"component_id": "idem", "kind": "idempotency_algorithm", **strategy_base},
        {"component_id": "eval", "kind": "evaluator", **strategy_base},
    ]
    command_entries: list[dict[str, object]] = []
    for index, (name, (shape, protocol)) in enumerate(_MATRIX.items()):
        plan: dict[str, str] = {"kind": "recover_scope_outcome"} if shape == "recover_scope_outcome" else {"kind": "fixed", "artifact_set_profile": shape}
        command_entries.append({
            "entry_kind": "command_profile", "command_id": f"cmd-{index}", "command_name": name,
            "command_version": "2.1.3", "profile_id": f"profile-{index}",
            "profile_schema_path": f"{common}/schema.json", "profile_schema_hash": schema_hash,
            "request_schema_path": f"{common}/schema.json", "request_schema_hash": schema_hash,
            "parameter_schema_uri": f"schema://command/{index}/parameters", "parameter_schema_path": f"{common}/schema.json", "parameter_schema_hash": schema_hash,
            "result_schema_uri": f"schema://command/{index}/result", "result_schema_path": f"{common}/schema.json", "result_schema_hash": schema_hash,
            "handler_id": "handler", "handler_version": "2.1.3", "allowed_scope_kinds": ["job"],
            "required_input_roles": [], "required_policy_roles": [], "lifecycle_slots": [], "required_capability": "runtime.execute_stage",
            "idempotency_algorithm_id": "idem", "idempotency_algorithm_version": "2.1.3",
            "idempotency_algorithm_contract_hash": raw_hash, "artifact_set_plan": plan, "commit_protocol": protocol,
            "side_effect_class": "store", **ownership,
        })
    for profile in sorted({shape for shape, _ in _MATRIX.values()} - {"absent", "recover_scope_outcome"}):
        command_entries.append({
            "entry_kind": "artifact_set_profile", "artifact_set_profile": profile,
            "decision_member_role": "decision", "decision_artifact_type": "decision_artifact",
            "required_member_roles": [{"role": "decision", "artifact_types": ["decision_artifact"], "scope_kinds": ["job"], "min_members": 1, "max_members": 1}],
            "conditional_member_roles": [], "forbidden_member_roles": [],
            "affected_chain_heads": [{"scope_kind": "job", "artifact_type": "decision_artifact"}],
            "forbidden_reference_directions": [], **ownership,
        })
    command_entries.extend([
        {
            "entry_kind": "authority_operation", "authority_kind": "dispatcher",
            "authority_id": "source-dispatcher", "contract_path": f"{common}/impl.md",
            "contract_hash": raw_hash, "allowed_artifact_types": ["decision_artifact"],
            **ownership,
        },
        {
            "entry_kind": "command_state_transition", "command_id": "cmd-0",
            "command_version": "2.1.3", "transition_id": "transition:complete",
            "from_state": "state:reserved", "to_state": "state:succeeded",
            "state_machine_schema_path": f"{common}/schema.json",
            "state_machine_schema_hash": schema_hash,
            "transition_schema_path": f"{common}/schema.json",
            "transition_schema_hash": schema_hash, **ownership,
        },
    ])
    recover = next(item for item in command_entries if item.get("command_name") == "BuildNarrativeGraph")
    command_entries.extend([
        {"entry_kind": "recover_scope_outcome", "strategy_id": "recover", "strategy_version": "2.1.3", "strategy_implementation_contract_hash": raw_hash, "outcome_branch": "business_effect", "commit_protocol": "recovery_outcome_protocol", "business_command_id": recover["command_id"], "business_command_version": "2.1.3", "business_artifact_set_profile": "stage_admission", "business_commit_protocol": "artifact_set_commit", **ownership},
        {"entry_kind": "recover_scope_outcome", "strategy_id": "recover", "strategy_version": "2.1.3", "strategy_implementation_contract_hash": raw_hash, "outcome_branch": "exhausted_evidence", "commit_protocol": "recovery_outcome_protocol", "artifact_set_profile": "recovery_exhausted_evidence", **ownership},
    ])
    strategies.append({"component_id": "recover", "kind": "recovery", **strategy_base})
    artifacts = [{
        "artifact_type": "decision_artifact", "payload_schema_path": f"{common}/schema.json", "payload_schema_hash": schema_hash,
        "envelope_schema_path": f"{common}/schema.json", "envelope_schema_hash": schema_hash,
        "allowed_scope_kinds": ["job"], "authority_writers": [{"kind": "dispatcher", "authority_id": "source-dispatcher"}],
        "permitted_producer_components": [{"component_id": "handler", "component_version": "2.1.3"}], "policy_requirements": {"kind": "none"}, **ownership,
    }]
    rules = [{"domain": "admission", "rule_id": "rule-1", "rule_class": "admission", "subject_artifact_types": ["decision_artifact"], "evaluator_component": "eval", "evaluator_component_version": "2.1.3", "evaluator_contract_hash": raw_hash, "indeterminate_allowed": False, "on_fail": "stop", "on_indeterminate": "stop", "allowed_recovery_kinds": [], "exhaustion_action": "stop", "diagnostic_schema_path": f"{common}/schema.json", "diagnostic_schema_hash": schema_hash, **ownership}]
    traces: list[dict[str, object]] = []
    subjects = [("rule", "rule-1"), ("artifact", "decision_artifact"), ("state_transition", "command:cmd-0@2.1.3#transition:complete")] + [("command", f"command:{entry['command_id']}@2.1.3") for entry in command_entries if entry.get("entry_kind") == "command_profile"]
    for index, (subject_kind, subject_id) in enumerate(subjects):
        traces.append({"entry_kind": "contract_trace", "contract_path": f"contract-{index}", "subject_kind": subject_kind, "subject_id": subject_id, "schema_path": f"{common}/schema.json", "schema_hash": schema_hash, "evaluator": {"component": "eval", "version": "2.1.3", "contract_hash": raw_hash}, "test_ids": {"pass": [f"p-{index}"], "fail": [f"f-{index}"], "indeterminate": []}, "rollout_gate": "contract_ci", **ownership})
        for prefix in ("p", "f"):
            traces.append({"entry_kind": "test_fixture_inventory", "test_id": f"{prefix}-{index}", "test_kind": "unit", "pack_id": "common-pack", "test_path": f"{common}/test.json", "test_file_hash": h(f"{common}/test.json"), "fixture_refs": [], **ownership})
    docs_by_kind = {"artifacts": artifacts, "commands": command_entries, "rules": rules, "strategies": strategies, "traces": traces}
    documents: list[dict[str, str]] = []
    for kind in sorted(docs_by_kind):
        body: dict[str, object] = {"format": "autocut.registry.source/v1", "registry_kind": kind, "contract_version": "2.1.3", "registry_version": "1.0.0", "pack_id": "source-pack", "entries": docs_by_kind[kind]}
        body["document_hash"] = canonical_json_hash(body)
        path = f"common/registries/{kind}.yaml"
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(_yaml(body) + "\n")
        documents.append({"registry_kind": kind, "path": path, "document_hash": body["document_hash"]})
    packs = []
    for order, kind in enumerate(("common", "commands", "stage_01", "stage_02", "stage_03", "stage_04", "stage_05", "publication")):
        entries = source_paths[kind]
        packs.append({"pack_id": f"{kind}-pack", "pack_order": order, "kind": kind, "root": kind, "source_paths": entries, "source_tree_hash": canonical_json_hash(sorted(entries, key=lambda item: item["path"].encode()))})
    manifest: dict[str, object] = {"format": "autocut.registry-set.source/v1", "contract_version": "2.1.3", "registry_set_version": "1.0.0", "pack_id": "source-pack", "source_packs": packs, "registry_documents": documents}
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    (root / "common/registry_set.yaml").write_text(_yaml(manifest) + "\n")
    return root


def _mutate_document_and_resign(
    root: Path, registry_kind: str, mutate: object
) -> None:
    """Mutate a semantic entry and re-sign both affected authority layers."""
    from autocut_kernel.contracts.compiler.registry_source import _load_yaml

    document_path = root / "common" / "registries" / f"{registry_kind}.yaml"
    document = _load_yaml(document_path)
    if not callable(mutate):
        raise AssertionError("test mutation must be callable")
    mutate(document["entries"])
    document_without_hash = {key: value for key, value in document.items() if key != "document_hash"}
    document["document_hash"] = canonical_json_hash(document_without_hash)
    document_path.write_text(_yaml(document) + "\n")

    manifest_path = root / "common" / "registry_set.yaml"
    manifest = _load_yaml(manifest_path)
    for entry in manifest["registry_documents"]:
        if entry["registry_kind"] == registry_kind:
            entry["document_hash"] = document["document_hash"]
            break
    else:
        raise AssertionError("missing registry document")
    manifest_without_hash = {key: value for key, value in manifest.items() if key != "registry_set_hash"}
    manifest["registry_set_hash"] = canonical_json_hash(manifest_without_hash)
    manifest_path.write_text(_yaml(manifest) + "\n")


def _mutate_profile_schema_and_resign(root: Path, mutate: object) -> None:
    """Change the exact profile schema and re-sign every dependent source layer."""
    from autocut_kernel.contracts.compiler.registry_source import _load_yaml

    if not callable(mutate):
        raise AssertionError("test mutation must be callable")
    schema_path = root / "common/schema.json"
    old_raw = schema_path.read_bytes()
    old_value = json.loads(old_raw)
    old_semantic_hash = canonical_json_hash(old_value)
    mutate(old_value)
    new_raw = json.dumps(old_value, separators=(",", ":")).encode() + b"\n"
    schema_path.write_bytes(new_raw)
    new_raw_hash = sha256_bytes(new_raw)
    new_semantic_hash = canonical_json_hash(old_value)

    def replace_hash(value: object) -> object:
        if value == old_semantic_hash:
            return new_semantic_hash
        if isinstance(value, list):
            return [replace_hash(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_hash(item) for key, item in value.items()}
        return value

    document_hashes: dict[str, str] = {}
    for kind in ("artifacts", "commands", "rules", "strategies", "traces"):
        path = root / "common/registries" / f"{kind}.yaml"
        document = replace_hash(_load_yaml(path))
        assert isinstance(document, dict)
        document.pop("document_hash")
        document["document_hash"] = canonical_json_hash(document)
        path.write_text(_yaml(document) + "\n")
        document_hashes[kind] = document["document_hash"]
    manifest_path = root / "common/registry_set.yaml"
    manifest = _load_yaml(manifest_path)
    for pack in manifest["source_packs"]:
        if pack["kind"] == "common":
            for entry in pack["source_paths"]:
                if entry["path"] == "common/schema.json":
                    entry["file_hash"] = new_raw_hash
            pack["source_tree_hash"] = canonical_json_hash(
                sorted(pack["source_paths"], key=lambda item: item["path"].encode())
            )
    for entry in manifest["registry_documents"]:
        entry["document_hash"] = document_hashes[entry["registry_kind"]]
    manifest.pop("registry_set_hash")
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    manifest_path.write_text(_yaml(manifest) + "\n")


def test_compiles_a_genuinely_closed_eight_pack_registry_ready(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    registry = compile_registry_source(_write_closed_source(tmp_path))
    assert registry.ready is True
    registry.require_ready()


def test_compiler_keeps_trace_contract_obligation_locator_outside_machine_path_grammar(
    tmp_path: Path,
) -> None:
    """A Trace obligation may remain human-readable while its owner proof is signed."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        trace = next(item for item in entries if item.get("entry_kind") == "contract_trace")
        assert isinstance(trace, dict)
        trace["contract_path"] = "原理/阶段-04#SA-DIALOGUE-001"

    _mutate_document_and_resign(root, "traces", mutate)
    assert compile_registry_source(root).ready


@pytest.mark.parametrize("locator", ["common//schema.json", "common/例.json"])
def test_compiler_rejects_re_signed_physical_entry_locator(
    tmp_path: Path, locator: str
) -> None:
    """A semantically re-signed Registry document cannot authorize an alias."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        entries[0]["payload_schema_path"] = locator

    _mutate_document_and_resign(root, "artifacts", mutate)
    with pytest.raises(RegistryValidationError, match="canonical ASCII|unsafe path"):
        compile_registry_source(root)


def test_compiler_rejects_a_forged_or_stale_manifest_snapshot(tmp_path: Path) -> None:
    from dataclasses import replace

    from autocut_kernel.contracts.compiler.registry import RegistrySet

    root = _write_closed_source(tmp_path)
    manifest = load_registry_source_manifest(root)
    with pytest.raises(RegistryValidationError, match="forged or stale"):
        RegistrySet.from_manifest(replace(manifest, registry_set_hash="sha256:" + "b" * 64))


def test_compiler_rejects_owner_proof_not_directly_listed_in_owner_pack(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    def mutate(entries: object) -> None:
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        entries[0]["owner_source_path"] = "commands/entry.json"

    _mutate_document_and_resign(root, "artifacts", mutate)
    with pytest.raises(RegistryValidationError, match="direct owner-pack inventory"):
        compile_registry_source(root)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda schema: schema.pop("x-autocut-role-declarations"), "role metadata is missing or not closed"),
        (lambda schema: schema["x-autocut-role-declarations"].update({"extra": []}), "role metadata is missing or not closed"),
        (lambda schema: schema["x-autocut-role-declarations"].update({"input": [{"role": "unexpected", "artifact_types": ["decision_artifact"], "scope_kinds": ["job"], "min_refs": 0, "max_refs": 1}]}), "required_input_roles do not exactly match"),
    ],
)
def test_compiler_rejects_profile_schema_metadata_drift(
    tmp_path: Path, mutate: object, needle: str
) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    _mutate_profile_schema_and_resign(root, mutate)
    with pytest.raises(RegistryValidationError, match=needle):
        compile_registry_source(root)


@pytest.mark.parametrize(
    ("registry_kind", "needle"),
    [
        ("artifacts", "duplicate artifact identity"),
        ("rules", "duplicate rule identity"),
        ("commands", "duplicate command profile_id"),
    ],
)
def test_compiler_rejects_duplicate_closed_identities_after_resigning(
    tmp_path: Path, registry_kind: str, needle: str
) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        if registry_kind == "commands":
            profiles = [item for item in entries if item["entry_kind"] == "command_profile"]
            profiles[1]["profile_id"] = profiles[0]["profile_id"]
            return
        else:
            original = entries[0]
        assert isinstance(original, dict)
        entries.append(dict(original))

    _mutate_document_and_resign(root, registry_kind, mutate)
    with pytest.raises(RegistryValidationError, match=needle):
        compile_registry_source(root)


def test_compiler_rejects_authority_operation_not_exactly_reciprocated(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        operation = next(item for item in entries if item["entry_kind"] == "authority_operation")
        operation["allowed_artifact_types"] = ["unreferenced_artifact"]

    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(RegistryValidationError, match="authority operation allowed artifacts"):
        compile_registry_source(root)


def test_compiler_resolves_transition_subject_before_accepting_trace(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        transition = next(
            item for item in entries if item["entry_kind"] == "command_state_transition"
        )
        transition["command_id"] = "unknown-command"

    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(RegistryValidationError, match="command transition does not resolve"):
        compile_registry_source(root)


def test_compiler_rejects_rule_subject_artifact_not_in_signed_inventory(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        entries[0]["subject_artifact_types"] = ["not_an_artifact"]

    _mutate_document_and_resign(root, "rules", mutate)
    with pytest.raises(RegistryValidationError, match="rule subject artifact type"):
        compile_registry_source(root)


@pytest.mark.parametrize(
    ("subject_kind", "expected"),
    [
        ("artifact", "trace indeterminate tests require an exact indeterminate-allowed rule"),
        ("rule", "trace indeterminate tests require an exact indeterminate-allowed rule"),
    ],
)
def test_compiler_rejects_signed_indeterminate_trace_without_exact_rule_authorization(
    tmp_path: Path, subject_kind: str, expected: str
) -> None:
    """Indeterminate tests are a Rule-only authorization, never trace-local policy."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        trace = next(
            item
            for item in entries
            if item["entry_kind"] == "contract_trace"
            and item["subject_kind"] == subject_kind
        )
        assert isinstance(trace, dict)
        tests = trace["test_ids"]
        assert isinstance(tests, dict)
        test_id = f"indeterminate-{subject_kind}"
        tests["indeterminate"] = [test_id]
        inventory = next(item for item in entries if item["entry_kind"] == "test_fixture_inventory")
        assert isinstance(inventory, dict)
        entries.append(inventory | {"test_id": test_id})

    _mutate_document_and_resign(root, "traces", mutate)
    with pytest.raises(RegistryValidationError, match=expected):
        compile_registry_source(root)


def test_compiler_requires_trace_evaluator_component_kind(tmp_path: Path) -> None:
    """A matching ID/version/hash is insufficient when it is not an evaluator."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        evaluator = next(item for item in entries if item["component_id"] == "eval")
        assert isinstance(evaluator, dict)
        evaluator["kind"] = "generator"

    _mutate_document_and_resign(root, "strategies", mutate)
    with pytest.raises(RegistryValidationError, match="trace evaluator is not an exact component"):
        compile_registry_source(root)


@pytest.mark.parametrize(
    ("registry_kind", "mutate", "needle"),
    [
        (
            "commands",
            lambda entries: next(
                item for item in entries if item["entry_kind"] == "command_profile"
            ).update({"required_capability": "runtime.unsafe"}),
            "closed v2.1.3 capability",
        ),
        (
            "strategies",
            lambda entries: next(
                item for item in entries if item["component_id"] == "eval"
            ).update({"capabilities": ["runtime.unsafe"]}),
            "closed v2.1.3 capability",
        ),
    ],
)
def test_compiler_rejects_unknown_signed_capability(
    tmp_path: Path, registry_kind: str, mutate: object, needle: str
) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    _mutate_document_and_resign(root, registry_kind, mutate)
    with pytest.raises(RegistryValidationError, match=needle):
        compile_registry_source(root)


def test_compiler_rejects_self_consistent_unknown_scope_after_resigning(tmp_path: Path) -> None:
    """Unknown scope remains forbidden even after every related source is re-signed."""
    from autocut_kernel.contracts.compiler.registry_source import (
        _load_yaml,
        compile_registry_source,
    )

    root = _write_closed_source(tmp_path)

    def mutate_schema(schema: object) -> None:
        assert isinstance(schema, dict)
        declarations = schema["x-autocut-role-declarations"]
        assert isinstance(declarations, dict)
        for field in ("input", "policy"):
            for item in declarations[field]:
                item["scope_kinds"] = ["unknown_scope"]

    _mutate_profile_schema_and_resign(root, mutate_schema)

    def mutate_artifacts(entries: object) -> None:
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        entries[0]["allowed_scope_kinds"] = ["unknown_scope"]

    def mutate_commands(entries: object) -> None:
        assert isinstance(entries, list)
        for entry in entries:
            if entry["entry_kind"] == "command_profile":
                entry["allowed_scope_kinds"] = ["unknown_scope"]
            elif entry["entry_kind"] == "artifact_set_profile":
                entry["required_member_roles"][0]["scope_kinds"] = ["unknown_scope"]
                entry["affected_chain_heads"][0]["scope_kind"] = "unknown_scope"

    _mutate_document_and_resign(root, "artifacts", mutate_artifacts)
    _mutate_document_and_resign(root, "commands", mutate_commands)
    # Demonstrate that the mutation was signed consistently; the compiler must
    # still reject it using the total-contract scope enum rather than fixtures.
    assert _load_yaml(root / "common/registry_set.yaml")["registry_set_hash"].startswith("sha256:")
    with pytest.raises(RegistryValidationError, match="closed v2.1.3 scope"):
        compile_registry_source(root)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda entries: next(
                item for item in entries if item["entry_kind"] == "artifact_set_profile"
            ).update({"forbidden_member_roles": ["not sorted", "decision"]}),
                "forbidden_member_roles must be sorted and unique",
        ),
        (
            lambda entries: next(
                item for item in entries if item["entry_kind"] == "artifact_set_profile"
            ).update(
                {
                    "affected_chain_heads": [
                        {"scope_kind": "job", "artifact_type": "decision_artifact"},
                        {"scope_kind": "job", "artifact_type": "decision_artifact"},
                    ]
                }
            ),
            "affected chain heads must be sorted and unique",
        ),
    ],
)
def test_compiler_rejects_signed_artifact_set_role_or_head_grammar_drift(
    tmp_path: Path, mutate: object, needle: str
) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(RegistryValidationError, match=needle):
        compile_registry_source(root)


def test_profile_metadata_can_only_be_read_from_verified_source_inventory(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    unlisted = root / "common" / "unlisted-profile.json"
    unlisted.write_bytes((root / "common" / "schema.json").read_bytes())
    unlisted_hash = canonical_json_hash(json.loads(unlisted.read_text()))

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        for entry in entries:
            if entry["entry_kind"] == "command_profile":
                entry["profile_schema_path"] = "common/unlisted-profile.json"
                entry["profile_schema_hash"] = unlisted_hash

    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(RegistryValidationError, match="profile schema cannot be read"):
        compile_registry_source(root)


def test_compiler_rejects_signed_duplicate_key_json_schema(tmp_path: Path) -> None:
    """A raw-byte-resigned duplicate JSON key cannot be silently overwritten."""
    from autocut_kernel.contracts.compiler.registry_source import (
        _load_yaml,
        compile_registry_source,
    )

    root = _write_closed_source(tmp_path)
    schema_path = root / "common/schema.json"
    raw = schema_path.read_bytes()
    schema_path.write_bytes(raw.replace(b'"type":"object"', b'"type":"object","type":"object"', 1))
    manifest_path = root / "common/registry_set.yaml"
    manifest = _load_yaml(manifest_path)
    for pack in manifest["source_packs"]:
        if pack["kind"] == "common":
            for source in pack["source_paths"]:
                if source["path"] == "common/schema.json":
                    source["file_hash"] = sha256_bytes(schema_path.read_bytes())
            pack["source_tree_hash"] = canonical_json_hash(
                sorted(pack["source_paths"], key=lambda item: item["path"].encode())
            )
    manifest.pop("registry_set_hash")
    manifest["registry_set_hash"] = canonical_json_hash(manifest)
    manifest_path.write_text(_yaml(manifest) + "\n")
    with pytest.raises(RegistryValidationError, match="profile schema cannot be read as JSON"):
        compile_registry_source(root)


def test_compiler_denies_whitespace_mutation_after_manifest_snapshot(tmp_path: Path) -> None:
    """RegistrySet revalidation prevents a manifest snapshot from crossing a TOCTOU gap."""
    from autocut_kernel.contracts.compiler.registry import RegistrySet

    root = _write_closed_source(tmp_path)
    manifest = load_registry_source_manifest(root)
    source = root / "common/entry.json"
    source.write_bytes(source.read_bytes() + b" \n")
    with pytest.raises(RegistryValidationError, match="file_hash"):
        RegistrySet.from_manifest(manifest)


def test_command_handler_must_have_exact_kind_and_declared_capability(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        handler = next(item for item in entries if item["component_id"] == "handler")
        handler["kind"] = "generator"

    _mutate_document_and_resign(root, "strategies", mutate)
    with pytest.raises(RegistryValidationError, match="dangling command handler"):
        compile_registry_source(root)


def test_command_handler_must_declare_each_profile_capability(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        handler = next(item for item in entries if item["component_id"] == "handler")
        handler["capabilities"] = ["media.prepare"]

    _mutate_document_and_resign(root, "strategies", mutate)
    with pytest.raises(RegistryValidationError, match="dangling command handler"):
        compile_registry_source(root)


def test_compiler_rejects_generic_state_schema_even_when_resigned(tmp_path: Path) -> None:
    """States may not be inferred from general JSON Schema structure."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    _mutate_profile_schema_and_resign(
        root, lambda schema: schema.pop("x-autocut-state-machine")
    )
    with pytest.raises(RegistryValidationError, match="state-machine metadata is missing"):
        compile_registry_source(root)


def test_compiler_rejects_unknown_or_mismatched_state_machine_edge(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        transition = next(item for item in entries if item["entry_kind"] == "command_state_transition")
        transition["to_state"] = "state:unknown"

    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(RegistryValidationError, match="does not exactly match state-machine declaration"):
        compile_registry_source(root)


def test_compiler_rejects_missing_transition_schema_metadata(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)
    _mutate_profile_schema_and_resign(
        root, lambda schema: schema.pop("x-autocut-transition")
    )
    with pytest.raises(RegistryValidationError, match="transition schema metadata is missing"):
        compile_registry_source(root)


def test_compiler_rejects_declared_state_edge_without_registry_or_trace(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(schema: object) -> None:
        assert isinstance(schema, dict)
        machine = schema["x-autocut-state-machine"]
        assert isinstance(machine, dict)
        machine["states"].append("state:terminal")
        machine["transitions"].append({
            "transition_id": "transition:terminalize",
            "from_state": "state:succeeded",
            "to_state": "state:terminal",
        })

    _mutate_profile_schema_and_resign(root, mutate)
    with pytest.raises(RegistryValidationError, match="one-to-one"):
        compile_registry_source(root)


def test_compiler_allows_explicit_self_loop_only_when_declared_everywhere(tmp_path: Path) -> None:
    """The authority permits explicit self loops; compiler must not invent or ban them."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate_schema(schema: object) -> None:
        assert isinstance(schema, dict)
        machine = schema["x-autocut-state-machine"]
        assert isinstance(machine, dict)
        transition = machine["transitions"][0]
        assert isinstance(transition, dict)
        transition["from_state"] = "state:succeeded"
        transition["to_state"] = "state:succeeded"
        exact = schema["x-autocut-transition"]
        assert isinstance(exact, dict)
        exact["from_state"] = "state:succeeded"
        exact["to_state"] = "state:succeeded"

    _mutate_profile_schema_and_resign(root, mutate_schema)

    def mutate_command(entries: object) -> None:
        assert isinstance(entries, list)
        transition = next(item for item in entries if item["entry_kind"] == "command_state_transition")
        transition["from_state"] = "state:succeeded"
        transition["to_state"] = "state:succeeded"

    _mutate_document_and_resign(root, "commands", mutate_command)
    # The trace identity retains transition_id but its schema remains the exact
    # transition schema; no special Runtime default is involved.
    assert compile_registry_source(root).ready


def test_compiler_rejects_wrong_state_transition_trace_subject(tmp_path: Path) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        trace = next(
            item
            for item in entries
            if item.get("entry_kind") == "contract_trace"
            and item.get("subject_kind") == "state_transition"
        )
        trace["subject_id"] = "command:cmd-0@2.1.3#transition:other"

    _mutate_document_and_resign(root, "traces", mutate)
    with pytest.raises(RegistryValidationError, match="state-machine transition requires exactly one trace"):
        compile_registry_source(root)


def test_compiler_rejects_signed_cross_family_state_machine_claim(tmp_path: Path) -> None:
    """One signed state-machine declaration cannot be owned by both families."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        command_transition = next(
            item for item in entries if item["entry_kind"] == "command_state_transition"
        )
        assert isinstance(command_transition, dict)
        entries.append(
            {
                "entry_kind": "artifact_state_transition",
                "artifact_type": "decision_artifact",
                "transition_id": command_transition["transition_id"],
                "from_state": command_transition["from_state"],
                "to_state": command_transition["to_state"],
                "state_machine_schema_path": command_transition["state_machine_schema_path"],
                "state_machine_schema_hash": command_transition["state_machine_schema_hash"],
                "transition_schema_path": command_transition["transition_schema_path"],
                "transition_schema_hash": command_transition["transition_schema_hash"],
                "owner_pack": command_transition["owner_pack"],
                "owner_source_path": command_transition["owner_source_path"],
                "owner_source_hash": command_transition["owner_source_hash"],
                "owner_contract_path": command_transition["owner_contract_path"],
                "owner_contract_hash": command_transition["owner_contract_hash"],
            }
        )

    _mutate_document_and_resign(root, "commands", mutate)
    with pytest.raises(
        RegistryValidationError,
        match="state-machine transition declaration is occupied by multiple families",
    ):
        compile_registry_source(root)


def test_compiler_rejects_signed_transition_trace_with_opposite_family(tmp_path: Path) -> None:
    """A command transition trace cannot be re-signed as an artifact subject."""
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        trace = next(
            item
            for item in entries
            if item.get("entry_kind") == "contract_trace"
            and item.get("subject_kind") == "state_transition"
        )
        assert isinstance(trace, dict)
        trace["subject_id"] = "artifact:decision_artifact#transition:complete"

    _mutate_document_and_resign(root, "traces", mutate)
    with pytest.raises(
        RegistryValidationError,
        match="state transition trace subject family does not match exact transition entry",
    ):
        compile_registry_source(root)


@pytest.mark.parametrize(
    "locator",
    [
        "#fragment",
        "stage-04#",
        "stage-04#first#second",
        "stage-04//nested#R",
        "stage-04/#R",
        "stage-04/../nested#R",
        "stage-04\\nested#R",
    ],
)
def test_compiler_rejects_re_signed_malformed_trace_contract_obligation_locator(
    tmp_path: Path, locator: str
) -> None:
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    root = _write_closed_source(tmp_path)

    def mutate(entries: object) -> None:
        assert isinstance(entries, list)
        trace = next(item for item in entries if item.get("entry_kind") == "contract_trace")
        assert isinstance(trace, dict)
        trace["contract_path"] = locator

    _mutate_document_and_resign(root, "traces", mutate)
    with pytest.raises(RegistryValidationError, match="contract_path"):
        compile_registry_source(root)
