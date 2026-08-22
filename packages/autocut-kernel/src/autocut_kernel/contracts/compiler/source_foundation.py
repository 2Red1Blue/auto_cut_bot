"""Fail-closed verification for the business-free Registry source foundation.

The foundation is consumed by later source-pack owners.  It consequently
freezes every input byte exactly once before interpreting it: a hash checked
from a second path read is not a provenance check, it is a TOCTOU bug.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .canonical import (
    CanonicalizationError,
    canonical_json_hash,
    load_canonical_json_bytes,
    sha256_bytes,
)
from .errors import RegistryValidationError

_HASH = re.compile(r"sha256:(?!0{64}$)[0-9a-f]{64}\Z")
_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SECTION_FRAGMENT = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*\Z")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_:-]*\Z")
_LIFECYCLE_SLOTS = frozenset({"initial", "recovery", "reconcile", "migration", "bootstrap"})
_HANDOFF_PATH = "handoff/source-foundation-handoff.json"
_REVIEW_PATH = "handoff/source-foundation-review.json"
_AUTHORITY_GIT_COMMIT = "794a4eb747e75cc7af0ca2f9b8cf8f173004ee5d"
_TEMPLATE_PATHS = (
    "handoff/handoff-manifest.schema.json",
    "schema-meta/owner-anchor.schema.json",
    "schema-meta/role-declarations.schema.json",
    "schema-meta/state-machine.schema.json",
    "schema-meta/transition.schema.json",
)

# A handoff may be independently re-signed by an attacker. These are the
# intentionally generic, authority-reviewed grammar templates, not a mutable
# source inventory. Updating one requires an explicit compiler review.
_TEMPLATE_RAW_HASHES = {
    "handoff/handoff-manifest.schema.json": "sha256:ea216b0a61685e98191b1a67bce75fb1aaf126c68b7a5aafc6dd27737f8c0d39",
    "schema-meta/owner-anchor.schema.json": "sha256:21cfd2eb5bc3ccda0cb28e2e92d046507b97758a823ef6e951e9aa5369443359",
    "schema-meta/role-declarations.schema.json": "sha256:ecf95b2afb2da39a053fbedfa5c40e539dbeb6e97de872ef5a75c0dbe208fcab",
    "schema-meta/state-machine.schema.json": "sha256:5240979aa6f7df60599ccc5cc362702bdb6be34ce57e35ea70be39ab8313ede9",
    "schema-meta/transition.schema.json": "sha256:5f1dc3633ec06e93f7bfa81b5a232a8c79cc62e7e6b3f2eea7913e51af787b9f",
}


@dataclass(frozen=True, slots=True)
class SourceFoundationTrustPin:
    """Externally supplied A1 producer and A2 attestation identities.

    No member is read from the handoff.  In particular, a re-signed handoff
    cannot authorize its own A2 commit: callers must pin the A1 producer, A2
    attestation checkout, and exact handoff bytes independently.
    """

    producer_git_commit: str
    attestation_git_commit: str
    handoff_raw_hash: str


@dataclass(frozen=True, slots=True)
class CapturedSource:
    """A single raw byte capture retained for all subsequent validation."""

    path: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class SourceFoundationHandoff:
    """Validated immutable source inventory for the A-batch templates."""

    source_root: Path
    source_paths: tuple[tuple[str, str], ...]
    source_tree_hash: str
    raw_inventory_hash: str
    authority_anchors: tuple[tuple[str, str], ...]
    source_snapshot: tuple[CapturedSource, ...]
    authority_snapshot: tuple[CapturedSource, ...]
    trust_pin: SourceFoundationTrustPin


def verify_source_foundation(
    source_root: Path,
    *,
    attestation_root: Path,
    authority_root: Path,
    trust_pin: SourceFoundationTrustPin,
    producer_git_checkout: Path,
    attestation_git_checkout: Path,
) -> SourceFoundationHandoff:
    """Verify a two-step A1 producer / A2 attestation provenance chain.

    ``source_root`` is read only from an A1 checkout whose ``HEAD`` is the
    pinned producer commit. ``attestation_root`` is read only from a distinct
    A2 checkout whose ``HEAD`` is the separately pinned attestation commit.
    Consumers must use the returned snapshots and must not reopen a source path
    after this function establishes the provenance binding.
    """

    root = _root(source_root, "source root")
    attestation = _root(attestation_root, "attestation root")
    authority = _root(authority_root, "authority root")
    _validate_trust_pin(trust_pin)
    producer_checkout = _root(producer_git_checkout, "producer git checkout")
    attestation_checkout = _root(attestation_git_checkout, "attestation git checkout")
    if producer_checkout == attestation_checkout:
        raise RegistryValidationError("A1 producer and A2 attestation checkouts must be distinct")
    source_prefix = _checkout_relative(root, producer_checkout, "source root")
    attestation_prefix = _checkout_relative(attestation, attestation_checkout, "attestation root")
    _verify_checkout_head(producer_checkout, trust_pin.producer_git_commit, "producer")
    _verify_checkout_head(attestation_checkout, trust_pin.attestation_git_commit, "attestation")
    _verify_distinct_two_step_commits(
        producer_checkout,
        attestation_checkout,
        trust_pin.producer_git_commit,
        trust_pin.attestation_git_commit,
    )
    authority_checkout, authority_prefix = _bind_authority_checkout(authority)
    _verify_checkout_head(authority_checkout, _AUTHORITY_GIT_COMMIT, "authority")
    source_fd = _open_root(root, "source root")
    attestation_fd = _open_root(attestation, "attestation root")
    try:
        return _verify_source_foundation_snapshot(
            root, source_prefix, attestation_prefix, source_fd, attestation_fd,
            producer_checkout, attestation_checkout, authority_checkout, authority_prefix, trust_pin,
        )
    finally:
        os.close(attestation_fd)
        os.close(source_fd)


def _verify_source_foundation_snapshot(
    root: Path, source_prefix: str, attestation_prefix: str,
    source_fd: int,
    attestation_fd: int,
    producer_git_checkout: Path,
    attestation_git_checkout: Path,
    authority_git_checkout: Path,
    authority_prefix: str,
    trust_pin: SourceFoundationTrustPin,
) -> SourceFoundationHandoff:
    """Validate exclusively through the root descriptors captured by caller."""

    handoff_raw, _handoff_identity = _capture_raw_at(attestation_fd, _HANDOFF_PATH, "handoff")
    _verify_attestation_git_blob(
        attestation_git_checkout, trust_pin.attestation_git_commit, attestation_prefix,
        _HANDOFF_PATH, handoff_raw,
    )
    if sha256_bytes(handoff_raw) != trust_pin.handoff_raw_hash:
        raise RegistryValidationError("handoff raw hash does not match external trust pin")
    value = _object(_parse_json(handoff_raw, "handoff"), "handoff")
    _exact(
        value,
        {
            "format", "contract_version", "handoff_version", "producer", "authority_anchors",
            "source_paths", "source_tree_hash", "review",
        },
        "handoff",
    )
    if value["format"] != "autocut.registry-source.handoff/v1" or value["contract_version"] != "2.1.3":
        raise RegistryValidationError("handoff format or contract_version is invalid")
    producer = _object(value["producer"], "handoff.producer")
    _exact(producer, {"pack_id", "source_revision", "producer_git_commit"}, "handoff.producer")
    if producer["pack_id"] != "source_foundation":
        raise RegistryValidationError("handoff producer.pack_id must be source_foundation")
    _digest(producer["source_revision"], "handoff.producer.source_revision")
    producer_git_commit = _git_commit(producer["producer_git_commit"], "handoff.producer.producer_git_commit")
    if producer_git_commit != trust_pin.producer_git_commit:
        raise RegistryValidationError("handoff producer_git_commit does not match external trust pin")
    review = _object(value["review"], "handoff.review")
    _exact(review, {"reviewer_id", "review_record_hash", "producer_git_commit"}, "handoff.review")
    _text(review["reviewer_id"], "handoff.review.reviewer_id")
    review_hash = _digest(review["review_record_hash"], "handoff.review.review_record_hash")
    if _git_commit(review["producer_git_commit"], "handoff.review.producer_git_commit") != producer_git_commit:
        raise RegistryValidationError("handoff review must bind the producer_git_commit")

    source_paths = _path_hash_index(value["source_paths"], "handoff.source_paths")
    if tuple(path for path, _ in source_paths) != tuple(sorted((path for path, _ in source_paths), key=_utf8)):
        raise RegistryValidationError("handoff.source_paths must be UTF-8 sorted")
    if _HANDOFF_PATH in dict(source_paths) or _REVIEW_PATH in dict(source_paths):
        raise RegistryValidationError("handoff and review record must not be in the self-bound raw inventory")
    if tuple(path for path, _ in source_paths) != _TEMPLATE_PATHS:
        raise RegistryValidationError("handoff.source_paths must be exactly the five A1 foundation templates")
    inventory_snapshot, inventory_identities = _capture_inventory(source_fd, source_paths, "handoff.source_paths")
    source_bytes = {item.path: item.raw for item in inventory_snapshot}
    for path, digest in source_paths:
        if sha256_bytes(source_bytes[path]) != digest:
            raise RegistryValidationError(f"handoff source raw hash mismatch: {path}")
    tree_hash = _digest(value["source_tree_hash"], "handoff.source_tree_hash")
    tree_view = [{"path": path, "file_hash": digest} for path, digest in source_paths]
    if canonical_json_hash(tree_view) != tree_hash:
        raise RegistryValidationError("handoff source_tree_hash does not match its raw inventory")
    if producer["source_revision"] != tree_hash:
        raise RegistryValidationError("handoff producer.source_revision must bind the source tree hash")
    raw_inventory_hash = canonical_json_hash({"source_paths": tree_view})
    _verify_producer_git_blobs(
        producer_git_checkout, producer_git_commit, source_prefix,
        source_paths, source_bytes, tree_hash,
    )

    anchors = _path_hash_index(value["authority_anchors"], "handoff.authority_anchors", allow_fragment=True)
    authority_snapshot: list[CapturedSource] = []
    authority_cache: dict[str, bytes] = {}
    for locator, digest in anchors:
        path, fragment = _authority_locator(locator)
        raw = authority_cache.get(path)
        if raw is None:
            git_path = f"{authority_prefix}/{path}" if authority_prefix else path
            raw = _git_blob(
                authority_git_checkout,
                _AUTHORITY_GIT_COMMIT,
                git_path,
                label=f"authority anchor {locator}",
                allow_unicode_path=True,
            )
            authority_cache[path] = raw
        _resolve_authority_heading(raw, fragment, locator)
        if sha256_bytes(raw) != digest:
            raise RegistryValidationError(f"handoff authority anchor raw hash mismatch: {locator}")
        authority_snapshot.append(CapturedSource(locator, raw))

    review_raw, _review_identity = _capture_raw_at(attestation_fd, _REVIEW_PATH, "review record")
    _verify_attestation_git_blob(
        attestation_git_checkout, trust_pin.attestation_git_commit, attestation_prefix,
        _REVIEW_PATH, review_raw,
    )
    _reject_identity_aliases(
        {*inventory_identities.items()}, "A1 source paths",
    )
    _validate_review_record(
        review_raw, review_hash, source_tree_hash=tree_hash,
        raw_inventory_hash=raw_inventory_hash, authority_anchors=anchors,
        producer_git_commit=producer_git_commit,
    )
    _consume_templates(source_bytes)
    source_snapshot = (
        CapturedSource(_HANDOFF_PATH, handoff_raw),
        *inventory_snapshot,
        CapturedSource(_REVIEW_PATH, review_raw),
    )
    return SourceFoundationHandoff(
        root, source_paths, tree_hash, raw_inventory_hash, anchors, source_snapshot,
        tuple(authority_snapshot), trust_pin,
    )


def validate_role_declarations(value: object) -> None:
    """Validate closed role metadata without introducing business identities."""

    declaration = _object(value, "role declarations")
    _exact(declaration, {"input", "policy", "lifecycle_slots"}, "role declarations")
    for group in ("input", "policy"):
        roles = _array(declaration[group], f"role declarations.{group}")
        observed: list[str] = []
        for index, item in enumerate(roles):
            role = _object(item, f"role declarations.{group}[{index}]")
            _exact(role, {"role", "artifact_types", "scope_kinds", "min_refs", "max_refs"}, f"role declarations.{group}[{index}]")
            observed.append(_stable_id(role["role"], f"role declarations.{group}[{index}].role"))
            _sorted_unique_ids(role["artifact_types"], f"role declarations.{group}[{index}].artifact_types")
            _sorted_unique_ids(role["scope_kinds"], f"role declarations.{group}[{index}].scope_kinds")
            minimum = _nonnegative_int(role["min_refs"], f"role declarations.{group}[{index}].min_refs")
            maximum = _nonnegative_int(role["max_refs"], f"role declarations.{group}[{index}].max_refs")
            if minimum > maximum:
                raise RegistryValidationError(f"role declarations.{group}[{index}] min_refs exceeds max_refs")
        _sorted_unique(observed, f"role declarations.{group} roles")
    slots = _array(declaration["lifecycle_slots"], "role declarations.lifecycle_slots")
    if any(slot not in _LIFECYCLE_SLOTS for slot in slots):
        raise RegistryValidationError("role declarations.lifecycle_slots contains an invalid slot")
    _sorted_unique(cast(list[str], slots), "role declarations.lifecycle_slots")


def validate_state_machine(value: object) -> None:
    """Validate deterministic state/transition metadata without runtime defaults."""

    machine = _object(value, "state machine")
    _exact(machine, {"states", "transitions"}, "state machine")
    states = _array(machine["states"], "state machine.states")
    if not states:
        raise RegistryValidationError("state machine.states must not be empty")
    for index, state in enumerate(states):
        if not isinstance(state, str) or not state.startswith("state:") or not _ID.fullmatch(state):
            raise RegistryValidationError(f"state machine.states[{index}] is invalid")
    _sorted_unique(cast(list[str], states), "state machine.states")
    state_set = frozenset(cast(list[str], states))
    transitions = _array(machine["transitions"], "state machine.transitions")
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    observed: list[tuple[str, str, str]] = []
    for index, item in enumerate(transitions):
        transition = _object(item, f"state machine.transitions[{index}]")
        _exact(transition, {"transition_id", "from_state", "to_state"}, f"state machine.transitions[{index}]")
        transition_id, start, end = transition["transition_id"], transition["from_state"], transition["to_state"]
        if not isinstance(transition_id, str) or not transition_id.startswith("transition:") or not _ID.fullmatch(transition_id):
            raise RegistryValidationError(f"state machine.transitions[{index}].transition_id is invalid")
        if start not in state_set or end not in state_set:
            raise RegistryValidationError(f"state machine.transitions[{index}] endpoint is not a declared state")
        edge = (cast(str, start), cast(str, end))
        if transition_id in seen_ids or edge in seen_edges:
            raise RegistryValidationError(f"state machine.transitions[{index}] has a duplicate identity or edge")
        seen_ids.add(transition_id)
        seen_edges.add(edge)
        observed.append((transition_id, *edge))
    if observed != sorted(observed, key=lambda item: tuple(_utf8(part) for part in item)):
        raise RegistryValidationError("state machine.transitions must be UTF-8 tuple sorted")


def _consume_templates(source_bytes: Mapping[str, bytes]) -> None:
    for relative in _TEMPLATE_PATHS:
        raw = source_bytes[relative]
        if sha256_bytes(raw) != _TEMPLATE_RAW_HASHES[relative]:
            raise RegistryValidationError(f"template {relative} does not match its fixed approved shape")
        schema = _object(_parse_json(raw, f"template {relative}"), f"template {relative}")
        _validate_template_schema(relative, schema)


def _validate_template_schema(relative: str, schema: dict[str, Any]) -> None:
    expected_id = f"https://autocut.invalid/contracts/2.1.3/common/{relative}"
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema" or schema.get("$id") != expected_id:
        raise RegistryValidationError(f"template {relative} has an invalid schema identity")
    comment = schema.get("$comment")
    if not isinstance(comment, str) or not comment.startswith("Authority:"):
        raise RegistryValidationError(f"template {relative} lacks an Authority binding")
    _validate_closed_schema_node(schema, f"template {relative}")


def _validate_closed_schema_node(value: object, label: str) -> None:
    if isinstance(value, list):
        sequence = cast(list[object], value)
        for index, item in enumerate(sequence):
            _validate_closed_schema_node(item, f"{label}[{index}]")
        return
    if not isinstance(value, dict):
        return
    mapping = cast(dict[str, object], value)
    if "default" in mapping or "examples" in mapping:
        raise RegistryValidationError(f"{label} must not supply defaults or examples")
    if mapping.get("type") == "object" and "propertyNames" in mapping:
        if set(mapping) - {"type", "minProperties", "propertyNames", "additionalProperties"}:
            raise RegistryValidationError(f"{label} must be a fixed-shape constrained map")
    elif mapping.get("type") == "object" and "$ref" not in mapping:
        properties = mapping.get("properties")
        required = mapping.get("required")
        if type(properties) is not dict or type(required) is not list or mapping.get("additionalProperties") is not False:  # noqa: E721
            raise RegistryValidationError(f"{label} must be a closed required object")
        if set(cast(dict[str, object], properties)) != set(cast(list[object], required)):
            raise RegistryValidationError(f"{label} properties and required fields must match exactly")
    for key, item in mapping.items():
        if key not in {"$comment", "$id", "$schema"}:
            _validate_closed_schema_node(item, f"{label}.{key}")


def _validate_review_record(
    raw: bytes, expected_hash: str, *, source_tree_hash: str, raw_inventory_hash: str,
    authority_anchors: tuple[tuple[str, str], ...], producer_git_commit: str,
) -> None:
    if sha256_bytes(raw) != expected_hash:
        raise RegistryValidationError("review record raw hash mismatch")
    record = _object(_parse_json(raw, "review record"), "review record")
    _exact(
        record,
        {
            "format", "reviewer_id", "scope", "status", "source_tree_hash", "raw_inventory_hash",
            "authority_anchors", "producer_git_commit",
        },
        "review record",
    )
    if record["format"] != "autocut.registry-source.review/v1" or record["scope"] != "source_foundation" or record["status"] != "approved":
        raise RegistryValidationError("review record has an invalid fixed identity")
    _text(record["reviewer_id"], "review record.reviewer_id")
    if _git_commit(record["producer_git_commit"], "review record.producer_git_commit") != producer_git_commit:
        raise RegistryValidationError("review record does not bind the trusted producer_git_commit")
    if record["source_tree_hash"] != source_tree_hash or record["raw_inventory_hash"] != raw_inventory_hash:
        raise RegistryValidationError("review record does not bind the frozen source tree and raw inventory")
    if _path_hash_index(record["authority_anchors"], "review record.authority_anchors", allow_fragment=True) != authority_anchors:
        raise RegistryValidationError("review record authority anchors do not match the handoff")


def _root(value: Path, label: str) -> Path:
    try:
        root = value.resolve(strict=True)
    except OSError as error:
        raise RegistryValidationError(f"{label} does not exist") from error
    if value.is_symlink() or not root.is_dir():
        raise RegistryValidationError(f"{label} must be a non-symlink directory")
    return root


def _capture_inventory(
    root_fd: int,
    source_paths: tuple[tuple[str, str], ...],
    label: str,
) -> tuple[tuple[CapturedSource, ...], dict[str, tuple[int, int]]]:
    """Capture an inventory once, rejecting two spellings of one file."""

    captures: list[CapturedSource] = []
    identities: dict[str, tuple[int, int]] = {}
    seen: dict[tuple[int, int], str] = {}
    for path, _digest_value in source_paths:
        raw, identity = _capture_raw_at(root_fd, path, f"{label}[{path}]")
        previous = seen.setdefault(identity, path)
        if previous != path:
            raise RegistryValidationError("source paths must not use physical-path aliases")
        captures.append(CapturedSource(path, raw))
        identities[path] = identity
    return tuple(captures), identities


def _reject_identity_aliases(items: set[tuple[str, tuple[int, int]]], label: str) -> None:
    seen: dict[tuple[int, int], str] = {}
    for path, identity in items:
        previous = seen.setdefault(identity, path)
        if previous != path:
            raise RegistryValidationError(f"{label} must not use physical-path aliases")


def _capture_raw_at(
    root_fd: int, relative: str, label: str, *, allow_unicode_path: bool = False,
) -> tuple[bytes, tuple[int, int]]:
    """Read one contained regular file through an already frozen root FD."""

    if allow_unicode_path:
        _safe_physical_path(relative, label)
    else:
        _safe_path(relative, label)
    current_fd = os.dup(root_fd)
    try:
        parts = relative.split("/")
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if not leaf:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as error:
                raise RegistryValidationError(f"{label}: path escapes root or cannot be opened safely") from error
            os.close(current_fd)
            current_fd = next_fd
            mode = os.fstat(current_fd).st_mode
            if (leaf and not stat.S_ISREG(mode)) or (not leaf and not stat.S_ISDIR(mode)):
                raise RegistryValidationError(f"{label}: source must be a regular non-symlink file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(current_fd, 1024 * 1024)
            if not chunk:
                details = os.fstat(current_fd)
                return b"".join(chunks), (details.st_dev, details.st_ino)
            chunks.append(chunk)
    finally:
        os.close(current_fd)


def _open_root(root: Path, label: str) -> int:
    try:
        descriptor = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise RegistryValidationError(f"{label}: cannot open root safely") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RegistryValidationError(f"{label}: root is not a directory")
    return descriptor


def _parse_json(raw: bytes, label: str) -> Any:
    try:
        value, _canonical = load_canonical_json_bytes(raw, origin=label)
    except CanonicalizationError as error:
        raise RegistryValidationError(str(error)) from error
    return value


def _path_hash_index(value: object, label: str, *, allow_fragment: bool = False) -> tuple[tuple[str, str], ...]:
    mapping = _object(value, label)
    if not mapping:
        raise RegistryValidationError(f"{label} must not be empty")
    pairs: list[tuple[str, str]] = []
    for path, digest in mapping.items():
        if allow_fragment:
            _authority_locator(path)
        else:
            _safe_path(path, label)
        pairs.append((path, _digest(digest, f"{label}[{path}]")))
    return tuple(pairs)


def _authority_locator(value: str) -> tuple[str, str]:
    if value.count("#") != 1:
        raise RegistryValidationError("authority anchor must contain exactly one fragment separator")
    encoded_path, fragment = value.split("#", 1)
    path = _decode_authority_path(encoded_path)
    if not _SECTION_FRAGMENT.fullmatch(fragment):
        raise RegistryValidationError("authority anchor fragment must be a numbered Markdown section")
    return path, fragment


def _decode_authority_path(value: str) -> str:
    """Decode a canonical ASCII percent-encoded authority locator path.

    Authority files can have non-ASCII filesystem names, but their registry
    locators cannot.  Percent encoding gives one unambiguous ASCII spelling;
    raw Unicode, bidi controls, mixed escape spellings and normalization aliases
    are rejected before a filesystem lookup occurs.
    """

    _safe_path(value, "authority anchor")
    try:
        decoded = urllib.parse.unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RegistryValidationError("authority anchor path must contain canonical UTF-8 percent escapes") from error
    if urllib.parse.quote(decoded, safe="/-._~") != value:
        raise RegistryValidationError("authority anchor path must use canonical ASCII percent encoding")
    _safe_physical_path(decoded, "authority anchor")
    return decoded


def _resolve_authority_heading(raw: bytes, fragment: str, locator: str) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RegistryValidationError(f"authority anchor {locator}: Markdown is not UTF-8") from error
    headings = 0
    fence: tuple[str, int] | None = None
    comment_open = False
    heading = re.compile(rf"^[ ]{{0,3}}#{{1,6}}[ \t]+{re.escape(fragment)}(?=[ \t#]*$|[ \t])")
    for line in text.splitlines():
        fence_match = re.match(r"^[ ]{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= fence[1]:
                fence = None
            continue
        if fence_match:
            token = fence_match.group(1)
            fence = (token[0], len(token))
            continue
        visible, comment_open = _strip_html_comments(line, comment_open)
        if heading.match(visible):
            headings += 1
    if headings != 1:
        raise RegistryValidationError(f"authority anchor {locator}: numbered heading must resolve exactly once")


def _strip_html_comments(line: str, comment_open: bool) -> tuple[str, bool]:
    """Remove HTML comment spans before considering a Markdown heading."""

    visible: list[str] = []
    index = 0
    while index < len(line):
        if comment_open:
            end = line.find("-->", index)
            if end < 0:
                return "".join(visible), True
            comment_open = False
            index = end + 3
            continue
        start = line.find("<!--", index)
        if start < 0:
            visible.append(line[index:])
            break
        visible.append(line[index:start])
        comment_open = True
        index = start + 4
    return "".join(visible), comment_open


def _safe_path(value: str, label: str) -> None:
    """Reject raw spelling tricks before any path normalization occurs."""

    if not value or value.startswith("/") or value.endswith("/") or "\\" in value:
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if not value.isascii() or any(ord(char) <= 0x1F or ord(char) == 0x7F for char in value):
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if any(char in "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\ufeff" for char in value):
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise RegistryValidationError(f"{label}: path is not contained")


def _safe_physical_path(value: str, label: str) -> None:
    """Contain a decoded path after its raw locator has already been canonicalized."""

    if not value or value.startswith("/") or value.endswith("/") or "\\" in value:
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in value):
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if any(char in "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\ufeff" for char in value):
        raise RegistryValidationError(f"{label}: path is not a safe POSIX relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise RegistryValidationError(f"{label}: path is not contained")


def _validate_trust_pin(value: SourceFoundationTrustPin) -> None:
    _git_commit(value.producer_git_commit, "trust pin producer_git_commit")
    _git_commit(value.attestation_git_commit, "trust pin attestation_git_commit")
    _digest(value.handoff_raw_hash, "trust pin handoff_raw_hash")


def _git_commit(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT_COMMIT.fullmatch(value):
        raise RegistryValidationError(f"{label} must be a full lowercase Git object ID")
    return value


def _verify_checkout_head(checkout: Path, expected: str, role: str) -> None:
    """Bind a supplied checkout without invoking a shell or accepting Git config output."""

    _verify_git_commit_object(checkout, expected, role)
    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError(f"{role} git checkout cannot establish HEAD") from error
    actual = completed.stdout.strip()
    if completed.returncode != 0 or not _GIT_COMMIT.fullmatch(actual) or actual != expected:
        raise RegistryValidationError(f"{role} git checkout HEAD does not match its pinned commit")


def _verify_git_commit_object(checkout: Path, commit: str, role: str) -> None:
    """Require the pinned ID to resolve to a commit, not merely a ref spelling."""

    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError(f"{role} git checkout cannot verify pinned commit object") from error
    if completed.returncode != 0:
        raise RegistryValidationError(f"{role} git checkout is missing pinned commit object")


def _verify_distinct_two_step_commits(
    producer_checkout: Path,
    attestation_checkout: Path,
    producer_commit: str,
    attestation_commit: str,
) -> None:
    """A2 must be a later attestation in a separate Git object database.

    A worktree clone is not an independent attestation boundary: it shares the
    same common Git directory and lets one mutable repository rewrite both A1
    and A2.  We also require the ordinary append-only relation A1 -> A2.
    """

    if producer_commit == attestation_commit:
        raise RegistryValidationError("A1 producer and A2 attestation commits must be distinct")
    producer_common = _git_common_dir(producer_checkout, "producer")
    attestation_common = _git_common_dir(attestation_checkout, "attestation")
    if producer_common == attestation_common:
        raise RegistryValidationError("A1 producer and A2 attestation must use distinct Git common directories")
    _verify_git_commit_object(attestation_checkout, producer_commit, "attestation")
    try:
        completed = subprocess.run(
            ("git", "-C", str(attestation_checkout), "merge-base", "--is-ancestor", producer_commit, attestation_commit),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError("A2 attestation cannot verify the A1-to-A2 ancestry relation") from error
    if completed.returncode != 0:
        raise RegistryValidationError("A1 producer commit must be an ancestor of the A2 attestation commit")


def _git_common_dir(checkout: Path, role: str) -> Path:
    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), "rev-parse", "--git-common-dir"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError(f"{role} git checkout cannot identify its common directory") from error
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw:
        raise RegistryValidationError(f"{role} git checkout cannot identify its common directory")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = checkout / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise RegistryValidationError(f"{role} git common directory does not exist") from error


def _bind_authority_checkout(authority_root: Path) -> tuple[Path, str]:
    """Return the actual authority repository and the supplied source prefix.

    The authority worktree itself is deliberately not trusted: every anchored
    Markdown byte is subsequently loaded from the immutable pinned Git blob.
    ``authority_root`` remains useful only to name which directory inside that
    repository owns locator-relative paths.
    """

    try:
        completed = subprocess.run(
            ("git", "-C", str(authority_root), "rev-parse", "--show-toplevel"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError("authority root cannot identify its Git repository top level") from error
    raw = completed.stdout.strip()
    if completed.returncode != 0 or not raw:
        raise RegistryValidationError("authority root cannot identify its Git repository top level")
    try:
        checkout = Path(raw).resolve(strict=True)
    except OSError as error:
        raise RegistryValidationError("authority Git repository top level does not exist") from error
    if not checkout.is_dir():
        raise RegistryValidationError("authority Git repository top level is not a directory")
    return checkout, _checkout_relative(authority_root, checkout, "authority root")


def _checkout_relative(root: Path, checkout: Path, label: str) -> str:
    """Return a raw ASCII Git path without resolving through a second root."""

    try:
        relative = root.relative_to(checkout)
    except ValueError as error:
        raise RegistryValidationError(f"{label} is not contained by its pinned Git checkout") from error
    text = relative.as_posix()
    if text == ".":
        return ""
    _safe_path(text, f"{label} Git path")
    return text


def _verify_producer_git_blobs(
    checkout: Path,
    producer_git_commit: str,
    source_prefix: str,
    source_paths: tuple[tuple[str, str], ...],
    source_bytes: Mapping[str, bytes],
    source_tree_hash: str,
) -> None:
    """Compare the one-shot A1 snapshot with exact blobs in its pinned tree."""

    _verify_exact_a1_template_tree(checkout, producer_git_commit, source_prefix)
    git_view: list[dict[str, str]] = []
    for relative, digest in source_paths:
        git_path = f"{source_prefix}/{relative}" if source_prefix else relative
        blob = _git_blob(checkout, producer_git_commit, git_path)
        if blob != source_bytes[relative]:
            raise RegistryValidationError(f"producer Git blob does not match captured A1 source: {relative}")
        if sha256_bytes(blob) != digest:
            raise RegistryValidationError(f"producer Git blob hash does not match A1 inventory: {relative}")
        git_view.append({"path": relative, "file_hash": sha256_bytes(blob)})
    if canonical_json_hash(git_view) != source_tree_hash:
        raise RegistryValidationError("producer Git blobs do not reproduce the captured A1 source tree")


def _verify_exact_a1_template_tree(checkout: Path, commit: str, source_prefix: str) -> None:
    """Require the A1-owned template roots to contain no unreviewed files.

    The shared ``common`` directory legitimately contains earlier primitive
    schemas owned by other packs.  A1 owns only ``schema-meta`` and its one
    handoff grammar, so the tree comparison is deliberately scoped to those
    roots rather than treating unrelated common primitives as foundation data.
    """

    expected = {
        f"{source_prefix}/{path}" if source_prefix else path
        for path in _TEMPLATE_PATHS
    }
    roots = (
        f"{source_prefix}/schema-meta" if source_prefix else "schema-meta",
        f"{source_prefix}/handoff" if source_prefix else "handoff",
    )
    actual = _git_tree_paths(checkout, commit, roots, "producer")
    if actual != expected:
        raise RegistryValidationError(
            "A1 producer template roots must contain exactly the five approved foundation templates"
        )


def _git_tree_paths(checkout: Path, commit: str, roots: tuple[str, ...], role: str) -> set[str]:
    for root in roots:
        _safe_path(root, f"{role} Git tree root")
    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), "ls-tree", "-r", "--name-only", commit, "--", *roots),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError(f"{role} Git object database cannot enumerate source tree") from error
    if completed.returncode != 0:
        raise RegistryValidationError(f"{role} Git object database cannot enumerate source tree")
    paths = {line for line in completed.stdout.splitlines() if line}
    for path in paths:
        _safe_path(path, f"{role} Git tree path")
    return paths


def _git_blob(
    checkout: Path,
    commit: str,
    path: str,
    *,
    label: str = "Git blob",
    allow_unicode_path: bool = False,
) -> bytes:
    """Read one exact Git blob, without trusting working-tree bytes or a shell."""

    if allow_unicode_path:
        _safe_physical_path(path, f"{label} path")
    else:
        _safe_path(path, f"{label} path")
    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), "cat-file", "blob", f"{commit}:{path}"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryValidationError(f"{label}: Git object database cannot read required blob") from error
    if completed.returncode != 0:
        raise RegistryValidationError(f"{label}: Git object database is missing required blob")
    return completed.stdout


def _verify_attestation_git_blob(
    checkout: Path, attestation_git_commit: str, attestation_prefix: str,
    relative: str, captured: bytes,
) -> None:
    """Attestation bytes are trusted only when committed in the pinned A2 tree."""

    git_path = f"{attestation_prefix}/{relative}" if attestation_prefix else relative
    try:
        blob = _git_blob(checkout, attestation_git_commit, git_path)
    except RegistryValidationError as error:
        raise RegistryValidationError(f"A2 attestation Git object database is missing required blob: {relative}") from error
    if blob != captured:
        raise RegistryValidationError(f"A2 attestation Git blob does not match captured bytes: {relative}")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise RegistryValidationError(f"{label} must be a non-zero lowercase sha256 digest")
    return value


def _object(value: object, label: str) -> dict[str, Any]:
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _array(value: object, label: str) -> list[Any]:
    if type(value) is not list:  # noqa: E721
        raise RegistryValidationError(f"{label} must be an array")
    return cast(list[Any], value)


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RegistryValidationError(f"{label} must have exactly {sorted(expected)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise RegistryValidationError(f"{label} must be a non-empty trimmed string")
    return value


def _stable_id(value: object, label: str) -> str:
    text = _text(value, label)
    if not _ID.fullmatch(text):
        raise RegistryValidationError(f"{label} must be a stable ID")
    return text


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:  # noqa: E721
        raise RegistryValidationError(f"{label} must be a non-negative integer")
    return value


def _sorted_unique_ids(value: object, label: str) -> None:
    values = _array(value, label)
    if not values:
        raise RegistryValidationError(f"{label} must not be empty")
    for item in values:
        _stable_id(item, label)
    _sorted_unique(cast(list[str], values), label)


def _sorted_unique(values: list[str], label: str) -> None:
    if values != sorted(values, key=_utf8) or len(set(values)) != len(values):
        raise RegistryValidationError(f"{label} must be UTF-8 sorted and unique")


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")
