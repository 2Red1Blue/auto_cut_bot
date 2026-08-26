"""Implementation-bundle identity tests, not proof of a deployed wheel's trust."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from importlib import resources
from pathlib import Path

import pytest
from autocut_kernel.vlm import parser_contract
from autocut_kernel.vlm.parser_contract import VlmParserContractError, vlm_parser_contract_sha256

# Deliberately independent of production constants: this is the frozen oracle.
MEMBERS = (
    "media/root_evidence.py", "media/types.py", "vlm/models.py",
    "vlm/parser.py", "vlm/window.py",
)
LIMIT = 4 * 1024 * 1024


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _oracle(sources: dict[str, bytes]) -> str:
    material = {
        "schema_version": "vlm-parser-implementation-contract-v1",
        "parser_strategy_version": "strict-semantic-pack-v3",
        "sources": [{"path": path, "sha256": _sha(sources[path])} for path in MEMBERS],
    }
    return _sha(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


@pytest.fixture
def synthetic_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = {path: f"# synthetic source for {path}\n".encode() for path in MEMBERS}
    for path, raw in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    calls = []

    def installed_root(package: str):
        calls.append(package)
        assert package == "autocut_kernel"
        return tmp_path

    monkeypatch.setattr(parser_contract.resources, "files", installed_root)
    return tmp_path, sources, calls


def test_installed_package_sources_are_readable_and_match_independent_oracle() -> None:
    root = resources.files("autocut_kernel")
    originals = {}
    for path in MEMBERS:
        with root.joinpath(*path.split("/")).open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        assert 0 < len(raw) <= LIMIT
        originals[path] = raw
    assert vlm_parser_contract_sha256() == _oracle(originals)


def test_fixed_paths_order_and_repeat_determinism(synthetic_sources) -> None:
    _, sources, calls = synthetic_sources
    assert MEMBERS == tuple(sorted(MEMBERS))
    assert vlm_parser_contract_sha256() == _oracle(sources)
    assert vlm_parser_contract_sha256() == _oracle(sources)
    assert calls == ["autocut_kernel", "autocut_kernel"]


@pytest.mark.parametrize("member", MEMBERS)
def test_every_member_byte_change_including_comments_invalidates_identity_without_cache(synthetic_sources, member) -> None:
    root, sources, calls = synthetic_sources
    original = vlm_parser_contract_sha256()
    sources[member] += b"# changed implementation bundle bytes\n"
    (root / member).write_bytes(sources[member])
    assert vlm_parser_contract_sha256() == _oracle(sources) != original
    assert len(calls) == 2


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("failure", ("missing", "empty", "oversized"))
def test_every_missing_empty_or_oversized_member_fails_closed(synthetic_sources, member, failure) -> None:
    root, _, calls = synthetic_sources
    path = root / member
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"" if failure == "empty" else b"x" * (LIMIT + 1))
    with pytest.raises(VlmParserContractError) as caught:
        vlm_parser_contract_sha256()
    assert str(root) not in str(caught.value)
    assert calls == ["autocut_kernel"]


def test_exact_per_member_limit_is_allowed(synthetic_sources) -> None:
    root, sources, _ = synthetic_sources
    sources["vlm/parser.py"] = b"x" * LIMIT
    (root / "vlm/parser.py").write_bytes(sources["vlm/parser.py"])
    assert vlm_parser_contract_sha256() == _oracle(sources)


def test_unrelated_files_and_creation_order_do_not_enter_the_bundle(synthetic_sources) -> None:
    root, sources, _ = synthetic_sources
    expected = _oracle(sources)
    for path in ("vlm/decoder.py", "vlm/provider_port.py", "vlm/retry_policy.py", "__init__.py"):
        (root / path).write_bytes(b"# unrelated installed source\n")
    for path in reversed(MEMBERS):
        (root / path).write_bytes(sources[path])
    assert vlm_parser_contract_sha256() == expected


@pytest.mark.parametrize("argument", ("path", "sources", "strategy_version", "package"))
def test_no_caller_source_or_strategy_overrides(synthetic_sources, argument) -> None:
    _, _, calls = synthetic_sources
    with pytest.raises(TypeError):
        vlm_parser_contract_sha256(**{argument: object()})
    assert calls == []


@pytest.mark.parametrize("failure", (FileNotFoundError("sensitive path"), PermissionError("sensitive path"), ModuleNotFoundError("sensitive package")))
def test_resource_root_failures_are_safe_value_errors(monkeypatch, failure) -> None:
    def unavailable(package):
        raise failure

    monkeypatch.setattr(parser_contract.resources, "files", unavailable)
    with pytest.raises(VlmParserContractError) as caught:
        vlm_parser_contract_sha256()
    assert isinstance(caught.value, ValueError)
    assert "sensitive" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("failure_stage", (None, "open", "read"))
def test_bounded_reads_and_stream_cleanup(monkeypatch, failure_stage) -> None:
    reads = []
    streams = []
    paths = []

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size == LIMIT + 1
            if failure_stage == "read":
                raise OSError("sensitive read failure")
            return super().read(size)

    class Member:
        def open(self, mode):
            assert mode == "rb"
            if failure_stage == "open":
                raise OSError("sensitive open failure")
            stream = BoundedStream(b"# bounded source\n")
            streams.append(stream)
            return stream

    class InstalledRoot:
        def joinpath(self, *parts):
            paths.append("/".join(parts))
            return Member()

    monkeypatch.setattr(parser_contract.resources, "files", lambda package: InstalledRoot())
    if failure_stage:
        with pytest.raises(VlmParserContractError, match="unavailable or unreadable"):
            vlm_parser_contract_sha256()
        assert paths == [MEMBERS[0]]
    else:
        assert vlm_parser_contract_sha256() == _oracle(dict.fromkeys(MEMBERS, b"# bounded source\n"))
        assert tuple(paths) == MEMBERS and reads == [LIMIT + 1] * len(MEMBERS)
    assert all(stream.closed for stream in streams)


def test_zip_backed_resources_need_no_filesystem_extraction(tmp_path: Path, monkeypatch) -> None:
    sources = {path: f"# zip member {path}\n".encode() for path in MEMBERS}
    archive_path = tmp_path / "synthetic-package.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in reversed(MEMBERS):
            archive.writestr("autocut_kernel/" + path, sources[path])
    with zipfile.ZipFile(archive_path) as archive:
        root = zipfile.Path(archive, "autocut_kernel/")
        monkeypatch.setattr(parser_contract.resources, "files", lambda package: root)
        assert vlm_parser_contract_sha256() == _oracle(sources)
    assert list(tmp_path.iterdir()) == [archive_path]
