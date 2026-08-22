# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Fail-closed loader for the v2.1.3 Registry source-manifest envelope.

This module deliberately validates source provenance only.  Registry entry
grammars and cross-document closure belong to later compiler stages.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .canonical import canonical_json_hash, sha256_bytes
from .errors import RegistryValidationError
from .registry_entries import machine_source_locator

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_PACKS = (
    (0, "common"),
    (1, "commands"),
    (2, "stage_01"),
    (3, "stage_02"),
    (4, "stage_03"),
    (5, "stage_04"),
    (6, "stage_05"),
    (7, "publication"),
)
_REGISTRY_PATHS = {
    "artifacts": "common/registries/artifacts.yaml",
    "commands": "common/registries/commands.yaml",
    "rules": "common/registries/rules.yaml",
    "strategies": "common/registries/strategies.yaml",
    "traces": "common/registries/traces.yaml",
}
_MANIFEST_FIELDS = frozenset(
    {
        "format",
        "contract_version",
        "registry_set_version",
        "pack_id",
        "source_packs",
        "registry_documents",
        "registry_set_hash",
    }
)
_PACK_FIELDS = frozenset(
    {"pack_id", "pack_order", "kind", "root", "source_paths", "source_tree_hash"}
)
_SOURCE_PATH_FIELDS = frozenset({"path", "file_hash"})
_DOCUMENT_FIELDS = frozenset({"registry_kind", "path", "document_hash"})
_ENVELOPE_FIELDS = frozenset(
    {
        "format",
        "registry_kind",
        "contract_version",
        "registry_version",
        "pack_id",
        "entries",
        "document_hash",
    }
)


@dataclass(frozen=True, slots=True)
class SourcePath:
    path: str
    file_hash: str


@dataclass(frozen=True, slots=True)
class SourcePack:
    pack_id: str
    pack_order: int
    kind: str
    root: str
    source_paths: tuple[SourcePath, ...]
    source_tree_hash: str


@dataclass(frozen=True, slots=True)
class RegistryDocument:
    registry_kind: str
    path: str
    document_hash: str
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One raw byte binding captured while the signed manifest is verified."""

    path: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class RegistrySourceManifest:
    """Validated source-manifest snapshot; this is not an executable RegistrySet."""

    root: Path
    pack_id: str
    registry_set_version: str
    source_packs: tuple[SourcePack, ...]
    source_snapshot: tuple[SourceSnapshot, ...]
    registry_documents: tuple[RegistryDocument, ...]
    registry_set_hash: str


class _FixedRootReader:
    """Read one source tree through a pinned root descriptor.

    Every component is opened relative to the already-open parent with
    ``O_NOFOLLOW``.  The final bytes are read from that same descriptor after
    ``fstat`` proves it is a regular file; there is no resolve/check/reopen
    window for a symlink or directory swap to cross.
    """

    def __init__(self, source_root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(source_root)))
        self._root_fd: int | None = None

    def __enter__(self) -> "_FixedRootReader":
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise RegistryValidationError(
                "platform lacks descriptor-relative no-follow source reads"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = -1
        try:
            descriptor = os.open(self.root, flags)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                descriptor = -1
                raise RegistryValidationError("source root must be a non-symlink directory")
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise RegistryValidationError(
                "source root must be an existing non-symlink directory"
            ) from error
        self._root_fd = descriptor
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def read_bytes(self, relative: object, *, label: str) -> bytes:
        """Capture raw bytes from one exact machine locator under the pinned root."""
        locator = machine_source_locator(relative, label=f"{label}.path")
        if self._root_fd is None:
            raise RegistryValidationError("source root descriptor is not open")
        current_fd = os.dup(self._root_fd)
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        # Opening a FIFO read-only blocks until a writer connects.  Keep this
        # descriptor-relative preflight fail-closed: open non-blocking, then
        # reject every non-regular final node with fstat before attempting a
        # read.  O_NOFOLLOW alone does not protect this availability boundary.
        file_flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            parts = locator.split("/")
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                try:
                    is_directory = stat.S_ISDIR(os.fstat(next_fd).st_mode)
                except OSError:
                    os.close(next_fd)
                    raise
                if not is_directory:
                    os.close(next_fd)
                    raise RegistryValidationError(
                        f"{label}: source path component must be a directory"
                    )
                os.close(current_fd)
                current_fd = next_fd

            file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
            try:
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise RegistryValidationError(f"{label}: source must be a regular file")
                with os.fdopen(file_fd, "rb", closefd=True) as stream:
                    file_fd = -1
                    return stream.read()
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
        except OSError as error:
            raise RegistryValidationError(
                f"{label}: source path is missing, non-regular, or contains a symbolic link"
            ) from error
        finally:
            os.close(current_fd)


def load_registry_source_manifest(source_root: Path) -> RegistrySourceManifest:
    """Load and verify the fixed eight-pack source manifest rooted at ``source_root``."""

    with _FixedRootReader(source_root) as source:
        manifest_raw = source.read_bytes(
            "common/registry_set.yaml", label="registry manifest"
        )
        manifest = _load_yaml_bytes(
            manifest_raw, origin=f"{source.root}/common/registry_set.yaml"
        )
        _exact_fields(manifest, _MANIFEST_FIELDS, "registry_set")
        _require_text(manifest, "format", "registry_set")
        if manifest["format"] != "autocut.registry-set.source/v1":
            raise RegistryValidationError("registry_set.format is invalid")
        if manifest.get("contract_version") != "2.1.3":
            raise RegistryValidationError("registry_set.contract_version must be 2.1.3")
        version = _require_text(manifest, "registry_set_version", "registry_set")
        if not _SEMVER.fullmatch(version):
            raise RegistryValidationError("registry_set.registry_set_version must be SemVer")
        pack_id = _stable_id(manifest, "pack_id", "registry_set")
        _hash(manifest, "registry_set_hash", "registry_set")

        packs, snapshot = _parse_packs(source, manifest["source_packs"])
        documents = _parse_documents(source, manifest["registry_documents"], pack_id)
        view = dict(manifest)
        del view["registry_set_hash"]
        if canonical_json_hash(view) != manifest["registry_set_hash"]:
            raise RegistryValidationError("registry_set_hash does not match its JCS hash view")
        return RegistrySourceManifest(
            source.root,
            pack_id,
            version,
            packs,
            snapshot,
            documents,
            manifest["registry_set_hash"],
        )


def compile_registry_source(source_root: Path) -> object:
    """Compile exactly one manifest-validated source snapshot.

    This intentionally has no fallback to the repository source tree or an
    in-memory mapping: incomplete checked-in source is denied at the boundary.
    """
    from .registry import RegistrySet

    return RegistrySet.from_manifest(load_registry_source_manifest(source_root))


def _parse_packs(
    source: _FixedRootReader, value: object
) -> tuple[tuple[SourcePack, ...], tuple[SourceSnapshot, ...]]:
    if type(value) is not list or len(value) != len(_PACKS):  # noqa: E721
        raise RegistryValidationError("source_packs must contain exactly eight entries")
    packs: list[SourcePack] = []
    snapshot: list[SourceSnapshot] = []
    pack_ids: set[str] = set()
    for index, (expected_order, expected_kind) in enumerate(_PACKS):
        item = value[index]
        _exact_fields(item, _PACK_FIELDS, f"source_packs[{index}]")
        if (
            item["pack_order"] != expected_order
            or item["kind"] != expected_kind
            or item["root"] != expected_kind
        ):
            raise RegistryValidationError(
                "source_packs must use the fixed pack order, kind, and root"
            )
        identifier = _stable_id(item, "pack_id", f"source_packs[{index}]")
        if identifier in pack_ids:
            raise RegistryValidationError("source_packs pack_id is duplicated")
        pack_ids.add(identifier)
        _hash(item, "source_tree_hash", f"source_packs[{index}]")
        paths_value = item["source_paths"]
        if type(paths_value) is not list or not paths_value:
            raise RegistryValidationError("source_paths must be a non-empty array")
        paths: list[SourcePath] = []
        seen: set[str] = set()
        for path_index, source_entry in enumerate(paths_value):
            label = f"source_packs[{index}].source_paths[{path_index}]"
            _exact_fields(source_entry, _SOURCE_PATH_FIELDS, label)
            relative = _pack_path(source_entry.get("path"), expected_kind, label)
            if relative in seen:
                raise RegistryValidationError(f"{label}.path is duplicated")
            seen.add(relative)
            if relative == "common/registry_set.yaml" or relative in _REGISTRY_PATHS.values():
                raise RegistryValidationError(
                    "manifest and registry documents must not appear in source_paths"
                )
            declared_hash = _hash(source_entry, "file_hash", label)
            # Capture the bytes at verification time.  Later closure phases
            # consume this immutable value, never a second filesystem read.
            raw = source.read_bytes(relative, label=label)
            actual = sha256_bytes(raw)
            if actual != declared_hash:
                raise RegistryValidationError(f"{label}.file_hash does not match raw source bytes")
            paths.append(SourcePath(relative, declared_hash))
            snapshot.append(SourceSnapshot(relative, raw))
        tree_view = [
            {"path": item.path, "file_hash": item.file_hash}
            for item in sorted(paths, key=lambda p: p.path.encode("utf-8"))
        ]
        if canonical_json_hash(tree_view) != item["source_tree_hash"]:
            raise RegistryValidationError("source_tree_hash does not match sorted raw file hashes")
        packs.append(
            SourcePack(
                identifier,
                expected_order,
                expected_kind,
                expected_kind,
                tuple(paths),
                item["source_tree_hash"],
            )
        )
    return tuple(packs), tuple(snapshot)


def _parse_documents(
    source: _FixedRootReader, value: object, manifest_pack_id: str
) -> tuple[RegistryDocument, ...]:
    if type(value) is not list or len(value) != len(_REGISTRY_PATHS):  # noqa: E721
        raise RegistryValidationError("registry_documents must contain exactly five entries")
    documents: list[RegistryDocument] = []
    expected_kinds = sorted(_REGISTRY_PATHS, key=lambda key: key.encode("utf-8"))
    for index, expected_kind in enumerate(expected_kinds):
        item = value[index]
        _exact_fields(item, _DOCUMENT_FIELDS, f"registry_documents[{index}]")
        if (
            item.get("registry_kind") != expected_kind
            or item.get("path") != _REGISTRY_PATHS[expected_kind]
        ):
            raise RegistryValidationError(
                "registry_documents must use fixed UTF-8 sorted kinds and paths"
            )
        declared_hash = _hash(item, "document_hash", f"registry_documents[{index}]")
        document_path = _REGISTRY_PATHS[expected_kind]
        document = _load_yaml_bytes(
            source.read_bytes(document_path, label="registry document"),
            origin=f"{source.root}/{document_path}",
        )
        _exact_fields(document, _ENVELOPE_FIELDS, f"{expected_kind} registry")
        if (
            document["format"] != "autocut.registry.source/v1"
            or document["registry_kind"] != expected_kind
        ):
            raise RegistryValidationError(f"{expected_kind} registry envelope is invalid")
        if document["contract_version"] != "2.1.3" or document["pack_id"] != manifest_pack_id:
            raise RegistryValidationError(
                f"{expected_kind} registry contract_version or pack_id is invalid"
            )
        if not _SEMVER.fullmatch(_require_text(document, "registry_version", expected_kind)):
            raise RegistryValidationError(f"{expected_kind} registry_version must be SemVer")
        if type(document["entries"]) is not list or not document["entries"]:  # noqa: E721
            raise RegistryValidationError(f"{expected_kind} entries must be a non-empty array")
        _hash(document, "document_hash", expected_kind)
        view = dict(document)
        del view["document_hash"]
        actual_hash = canonical_json_hash(view)
        if declared_hash != document["document_hash"] or actual_hash != declared_hash:
            raise RegistryValidationError(
                f"{expected_kind} document_hash does not match its JCS hash view"
            )
        documents.append(
            RegistryDocument(expected_kind, _REGISTRY_PATHS[expected_kind], declared_hash, document)
        )
    return tuple(documents)


def _load_yaml(path: Path) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
    """Test/tool helper that also performs one descriptor-relative read."""
    with _FixedRootReader(path.parent) as source:
        raw = source.read_bytes(path.name, label=str(path))
    return _load_yaml_bytes(raw, origin=str(path))


def _load_yaml_bytes(raw: bytes, *, origin: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise RegistryValidationError(f"{origin}: invalid UTF-8 YAML") from error
    value = _RestrictedYaml(text, origin=origin).parse()
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError(f"{origin}: YAML root must be an object")
    return value


class _RestrictedYaml:
    """A small YAML profile: indentation, mappings, sequences, JSON scalars only.

    It intentionally rejects flow collections, comments, block strings, anchors,
    aliases, tags, merge keys, and every implicit YAML scalar beyond JSON values.
    """

    def __init__(self, raw: str, *, origin: str) -> None:
        self.origin = origin
        self.lines: list[tuple[int, str, int]] = []
        for number, raw_line in enumerate(raw.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if (
                raw_line.startswith("\t")
                or "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" "))]
            ):
                self._fail(number, "tabs are forbidden")
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            text = raw_line[indent:]
            if _has_forbidden_yaml_token(text):
                self._fail(number, "comments, anchors, aliases, and tags are forbidden")
            self.lines.append((indent, text, number))
        self.index = 0

    def parse(self) -> Any:
        if not self.lines:
            self._fail(1, "YAML document must not be empty")
        value = self._block(self.lines[0][0])
        if self.index != len(self.lines):
            self._fail(self.lines[self.index][2], "unexpected indentation")
        return value

    def _block(self, indent: int) -> Any:
        if self.index >= len(self.lines) or self.lines[self.index][0] != indent:
            self._fail(self.lines[self.index - 1][2] if self.index else 1, "missing nested value")
        return (
            self._sequence(indent)
            if self.lines[self.index][1].startswith("- ")
            else self._mapping(indent)
        )

    def _mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.index < len(self.lines):
            line_indent, text, line = self.lines[self.index]
            if line_indent < indent:
                break
            if line_indent != indent or text.startswith("- "):
                self._fail(line, "invalid mapping indentation")
            if ":" not in text:
                self._fail(line, "mapping key must have a value separator")
            key, raw_value = text.split(":", 1)
            if (
                not key
                or key.strip() != key
                or key == "<<"
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            ):
                self._fail(line, "mapping key is invalid or a merge key")
            if key in result:
                self._fail(line, f"duplicate YAML key {key!r}")
            self.index += 1
            result[key] = self._nested_or_scalar(raw_value, indent, line)
        return result

    def _sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while self.index < len(self.lines):
            line_indent, text, line = self.lines[self.index]
            if line_indent < indent:
                break
            if line_indent != indent or not text.startswith("- "):
                self._fail(line, "invalid sequence indentation")
            tail = text[2:]
            self.index += 1
            if not tail:
                result.append(self._required_child(indent, line))
            elif ":" in tail:
                key, raw_value = tail.split(":", 1)
                if (
                    not key
                    or key.strip() != key
                    or key == "<<"
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                ):
                    self._fail(line, "sequence mapping key is invalid")
                item = {key: self._nested_or_scalar(raw_value, indent, line)}
                if self.index < len(self.lines) and self.lines[self.index][0] > indent:
                    extra = self._mapping(self.lines[self.index][0])
                    if set(item) & set(extra):
                        self._fail(line, "duplicate YAML key in sequence mapping")
                    item.update(extra)
                result.append(item)
            else:
                result.append(self._scalar(tail, line))
        return result

    def _nested_or_scalar(self, raw_value: str, indent: int, line: int) -> Any:
        if raw_value == "":
            return self._required_child(indent, line)
        if not raw_value.startswith(" ") or raw_value.startswith("  "):
            self._fail(line, "mapping values require one separating space")
        return self._scalar(raw_value[1:], line)

    def _required_child(self, parent_indent: int, line: int) -> Any:
        if self.index >= len(self.lines) or self.lines[self.index][0] <= parent_indent:
            self._fail(line, "nested value is required")
        return self._block(self.lines[self.index][0])

    def _scalar(self, value: str, line: int) -> Any:
        # Empty collections are the only flow-style values admitted by this
        # restricted profile.  They are necessary to represent closed optional
        # arrays without inventing a null/default convention.
        if value == "[]":
            return []
        if value == "{}":
            return {}
        if not value or value.startswith(("{", "[", "|", ">", "'")) or value.endswith(":"):
            self._fail(line, "unsupported YAML scalar syntax")
        if value.startswith('"'):
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as error:
                self._fail(line, "invalid JSON string scalar")
                raise AssertionError from error
            if type(parsed) is not str:  # noqa: E721
                self._fail(line, "quoted scalar must be a JSON string")
            return parsed
        if value in {"true", "false"}:
            return value == "true"
        if value in {"null", "Null", "NULL", "~"}:
            self._fail(line, "null values are not declared")
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
            return int(value)
        if re.fullmatch(
            r"[-+]?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+|[0-9]+[eE][-+]?[0-9]+|\.inf|\.nan)", value, re.I
        ):
            self._fail(line, "YAML floats and non-JSON numeric scalars are forbidden")
        if any(character.isspace() for character in value):
            self._fail(line, "plain scalars cannot contain whitespace")
        return value

    def _fail(self, line: int, detail: str) -> None:
        raise RegistryValidationError(f"{self.origin}:{line}: {detail}")


def _has_forbidden_yaml_token(text: str) -> bool:
    """Reject YAML control tokens without mistaking JSON string content for syntax."""
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in {"#", "&", "*", "!"}:
            return True
    return False


def _pack_path(value: object, pack_root: str, label: str) -> str:
    """Validate a source locator before *any* path normalization occurs.

    The manifest signs the raw spelling of every source path.  Accepting a
    spelling that ``PurePosixPath`` later normalizes would make the signed
    locator differ from the filesystem lookup (for example ``a//b`` or
    ``a/./b``).  Keep this deliberately narrower than general filesystem
    paths: Registry source locators are canonical ASCII POSIX-relative names.
    """

    locator = machine_source_locator(value, label=f"{label}.path")
    # With the raw spelling checks above, this is intentionally an exact raw
    # containment test rather than a normalized-path approximation.
    if pack_root and not (locator == pack_root or locator.startswith(f"{pack_root}/")):
        raise RegistryValidationError(f"{label}.path must be contained in {pack_root}")
    return locator


def _exact_fields(value: object, expected: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError(f"{label} must be an object")
    mapping = cast(dict[str, Any], value)
    actual = frozenset(mapping)
    if actual != expected:
        raise RegistryValidationError(f"{label} must have exactly {sorted(expected)}")
    return mapping


def _require_text(mapping: dict[str, Any], field: str, label: str) -> str:
    value = mapping.get(field)
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise RegistryValidationError(f"{label}.{field} must be a non-empty trimmed string")
    return value


def _stable_id(mapping: dict[str, Any], field: str, label: str) -> str:
    value = _require_text(mapping, field, label)
    if any(character.isspace() for character in value):
        raise RegistryValidationError(f"{label}.{field} must be a stable whitespace-free ID")
    return value


def _hash(mapping: dict[str, Any], field: str, label: str) -> str:
    value = mapping.get(field)
    if type(value) is not str or not _SHA256.fullmatch(value) or value == f"sha256:{'0' * 64}":  # noqa: E721
        raise RegistryValidationError(f"{label}.{field} must be a real lowercase sha256 digest")
    return value
