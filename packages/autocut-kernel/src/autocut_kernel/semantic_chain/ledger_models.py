"""Closed Stage 1 coverage values, not admission or proof of input conservation.

The Ledger owns window selectors and seeds without self-referential hashes.
Only the later proof decides isolation. Counts, reference kinds and local joins
are checked here; exact external objects, real input conservation, assignment
truth, sufficient evidence and safety require independent evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from .member_refs import SemanticObjectRef, SemanticReferenceError

_T = TypeVar("_T")
_SAFE = 2**53 - 1
_KINDS = ("fact", "event", "source_window", "obligation")
_REASONS = (
    "unassigned",
    "summary_evidence_missing",
    "low_confidence",
    "identity_unresolved",
    "continuity_missing_context",
    "continuity_conflict",
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GRAPH_KINDS = (
    "entity",
    "fact",
    "beat",
    "obligation",
    "story_thread",
    "character",
    "character_state",
    "relationship",
    "question",
    "foreshadow",
)
_SEMANTIC = tuple(("narrative_graph", kind) for kind in _GRAPH_KINDS) + (
    ("event_card_set", "event"),
    ("whole_series_source_manifest", "source"),
)
_ASSIGNMENTS = tuple(("narrative_graph", kind) for kind in _GRAPH_KINDS) + (
    ("event_card_set", "event"),
    ("episode_digest_set", "episode_digest"),
)
_EVIDENCE = (
    *_SEMANTIC,
    ("whole_series_source_manifest", "source_window"),
    ("episode_digest_set", "episode_digest"),
    ("event_card_set", "source_range"),
    ("vlm_semantic_pack", "vlm_entity"),
    ("vlm_semantic_pack", "vlm_fact"),
    ("vlm_semantic_pack", "vlm_event"),
)
_DIAGNOSTICS = (("evidence_diagnostics", "diagnostic"), ("conflict_diagnostics", "diagnostic"))
_UNIT_OWNERS = {
    "fact": ("narrative_graph", "fact"),
    "event": ("event_card_set", "event"),
    "obligation": ("narrative_graph", "obligation"),
}


class LedgerModelError(ValueError):
    """A Ledger value violates its closed shape or structural invariants."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise LedgerModelError("ledger text must be a non-empty UTF-8 string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise LedgerModelError("ledger text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    result = _text(value)
    if _SHA.fullmatch(result) is None:
        raise LedgerModelError("ledger hash must be lowercase sha256")
    return result


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _SAFE:  # noqa: E721
        raise LedgerModelError("coverage count must be a non-negative safe integer")
    return value


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise LedgerModelError("ledger value must be a closed JSON object")
    mapping = cast(dict[str, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != set(keys):  # noqa: E721
        raise LedgerModelError("ledger value has missing or unknown fields")
    return mapping


def _tuple(value: object, item_type: type[_T]) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise LedgerModelError("ledger collections must be actual tuples")
    items = cast(tuple[object, ...], value)
    if any(type(item) is not item_type for item in items):
        raise LedgerModelError("ledger collection member has the wrong type")
    return cast(tuple[_T, ...], items)


def _array(value: object, parse: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise LedgerModelError("ledger wire collection must be a JSON array")
    return tuple(parse(item) for item in cast(list[object], value))


def _ids(value: object) -> tuple[str, ...]:
    items = _tuple(value, str)
    for item in items:
        _text(item)
    if len(set(items)) != len(items):
        raise LedgerModelError("ledger IDs must be unique")
    return tuple(sorted(items))


def _ref(value: object) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except SemanticReferenceError as error:
        raise LedgerModelError("ledger semantic reference is malformed") from error


def _owner(value: object, allowed: tuple[tuple[str, str], ...]) -> SemanticObjectRef:
    if type(value) is not SemanticObjectRef:  # noqa: E721
        raise LedgerModelError("ledger reference must be an exact SemanticObjectRef")
    if (value.member_ref.artifact_type, value.object_type) not in allowed:
        raise LedgerModelError("ledger reference has an unsupported member/object owner")
    if (
        value.member_ref.artifact_type == "vlm_semantic_pack"
        or value.object_type == "source_window"
    ):
        _hash(value.object_id)
    return value


def _refs(value: object, allowed: tuple[tuple[str, str], ...]) -> tuple[SemanticObjectRef, ...]:
    items = _tuple(value, SemanticObjectRef)
    for item in items:
        _owner(item, allowed)
    if len(set(items)) != len(items):
        raise LedgerModelError("ledger references must be unique")
    return tuple(sorted(items, key=lambda item: canonical_json_bytes(item.to_mapping())))


@dataclass(frozen=True, slots=True)
class LocalCoverageWindowRef:
    window_id: str

    def __post_init__(self) -> None:
        _text(self.window_id)

    def to_mapping(self) -> dict[str, object]:
        return {"reference_type": "local_coverage_window", "window_id": self.window_id}

    @classmethod
    def from_mapping(cls, value: object) -> LocalCoverageWindowRef:
        item = _closed(value, ("reference_type", "window_id"))
        if _text(item["reference_type"]) != "local_coverage_window":
            raise LedgerModelError("unsupported local coverage reference type")
        return cls(_text(item["window_id"]))


CoverageTarget = SemanticObjectRef | LocalCoverageWindowRef


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    window_id: str
    source_window_ref: SemanticObjectRef
    source_ref: SemanticObjectRef
    fact_refs: tuple[SemanticObjectRef, ...]
    event_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _text(self.window_id)
        _owner(self.source_window_ref, (("whole_series_source_manifest", "source_window"),))
        _owner(self.source_ref, (("whole_series_source_manifest", "source"),))
        if self.source_window_ref.member_ref != self.source_ref.member_ref:
            raise LedgerModelError("coverage source and window must share their exact owner")
        object.__setattr__(self, "fact_refs", _refs(self.fact_refs, (_UNIT_OWNERS["fact"],)))
        object.__setattr__(self, "event_refs", _refs(self.event_refs, (_UNIT_OWNERS["event"],)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "source_window_ref": self.source_window_ref.to_mapping(),
            "source_ref": self.source_ref.to_mapping(),
            "fact_refs": [ref.to_mapping() for ref in self.fact_refs],
            "event_refs": [ref.to_mapping() for ref in self.event_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoverageWindow:
        item = _closed(
            value, ("window_id", "source_window_ref", "source_ref", "fact_refs", "event_refs")
        )
        return cls(
            _text(item["window_id"]),
            _ref(item["source_window_ref"]),
            _ref(item["source_ref"]),
            _array(item["fact_refs"], _ref),
            _array(item["event_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class CoverageRow:
    coverage_id: str
    unit_type: str
    unit_ref: CoverageTarget
    resolution_status: str
    disposition: str
    assignment_refs: tuple[SemanticObjectRef, ...]
    evidence_refs: tuple[SemanticObjectRef, ...]
    diagnostic_refs: tuple[SemanticObjectRef, ...]
    taint_seed_id: str | None

    def __post_init__(self) -> None:
        _text(self.coverage_id)
        if _text(self.unit_type) not in _KINDS:
            raise LedgerModelError("unsupported coverage unit type")
        if self.unit_type == "source_window":
            if type(self.unit_ref) is not LocalCoverageWindowRef:  # noqa: E721
                raise LedgerModelError("coverage window units require a local selector")
        else:
            _owner(self.unit_ref, (_UNIT_OWNERS[self.unit_type],))
        resolution, disposition = _text(self.resolution_status), _text(self.disposition)
        legal = {
            "resolved": ("narrative", "supporting"),
            "unresolved": ("unassigned",),
            "conflicted": ("narrative", "supporting", "unassigned"),
        }
        if resolution not in legal or disposition not in legal[resolution]:
            raise LedgerModelError("illegal coverage resolution/disposition combination")
        object.__setattr__(self, "assignment_refs", _refs(self.assignment_refs, _ASSIGNMENTS))
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs, _EVIDENCE))
        object.__setattr__(self, "diagnostic_refs", _refs(self.diagnostic_refs, _DIAGNOSTICS))
        if resolution == "resolved":
            if self.taint_seed_id is not None:
                raise LedgerModelError("resolved coverage cannot own a taint seed")
        else:
            _text(self.taint_seed_id)
            if not self.diagnostic_refs:
                raise LedgerModelError("unresolved or conflicted coverage needs a diagnostic")

    def to_mapping(self) -> dict[str, object]:
        return {
            "coverage_id": self.coverage_id,
            "unit_type": self.unit_type,
            "unit_ref": self.unit_ref.to_mapping(),
            "resolution_status": self.resolution_status,
            "disposition": self.disposition,
            "assignment_refs": [ref.to_mapping() for ref in self.assignment_refs],
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
            "diagnostic_refs": [ref.to_mapping() for ref in self.diagnostic_refs],
            "taint_seed_id": self.taint_seed_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoverageRow:
        item = _closed(
            value,
            (
                "coverage_id",
                "unit_type",
                "unit_ref",
                "resolution_status",
                "disposition",
                "assignment_refs",
                "evidence_refs",
                "diagnostic_refs",
                "taint_seed_id",
            ),
        )
        kind = _text(item["unit_type"])
        target = (
            LocalCoverageWindowRef.from_mapping(item["unit_ref"])
            if kind == "source_window"
            else _ref(item["unit_ref"])
        )
        return cls(
            _text(item["coverage_id"]),
            kind,
            target,
            _text(item["resolution_status"]),
            _text(item["disposition"]),
            _array(item["assignment_refs"], _ref),
            _array(item["evidence_refs"], _ref),
            _array(item["diagnostic_refs"], _ref),
            None if item["taint_seed_id"] is None else _text(item["taint_seed_id"]),
        )


@dataclass(frozen=True, slots=True)
class TaintSeed:
    seed_id: str
    root_refs: tuple[SemanticObjectRef, ...]
    root_window_ids: tuple[str, ...]
    frontier_refs: tuple[SemanticObjectRef, ...]
    frontier_window_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.seed_id)
        object.__setattr__(self, "root_refs", _refs(self.root_refs, _SEMANTIC))
        object.__setattr__(self, "frontier_refs", _refs(self.frontier_refs, _SEMANTIC))
        for name in ("root_window_ids", "frontier_window_ids", "reason_codes"):
            object.__setattr__(self, name, _ids(getattr(self, name)))
        if not self.root_refs and not self.root_window_ids:
            raise LedgerModelError("taint seed needs at least one root")
        if not self.reason_codes:
            raise LedgerModelError("taint seed needs a reason code")
        if any(reason not in _REASONS for reason in self.reason_codes):
            raise LedgerModelError("unsupported taint seed reason code")
        if (
            {"identity_unresolved", "continuity_missing_context"}.intersection(self.reason_codes)
            and not self.frontier_refs
            and not self.frontier_window_ids
        ):
            raise LedgerModelError("unknown identity or context requires an explicit frontier")

    def to_mapping(self) -> dict[str, object]:
        return {
            "seed_id": self.seed_id,
            "root_refs": [ref.to_mapping() for ref in self.root_refs],
            "root_window_ids": list(self.root_window_ids),
            "frontier_refs": [ref.to_mapping() for ref in self.frontier_refs],
            "frontier_window_ids": list(self.frontier_window_ids),
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_mapping(cls, value: object) -> TaintSeed:
        item = _closed(
            value,
            (
                "seed_id",
                "root_refs",
                "root_window_ids",
                "frontier_refs",
                "frontier_window_ids",
                "reason_codes",
            ),
        )
        return cls(
            _text(item["seed_id"]),
            _array(item["root_refs"], _ref),
            _array(item["root_window_ids"], _text),
            _array(item["frontier_refs"], _ref),
            _array(item["frontier_window_ids"], _text),
            _array(item["reason_codes"], _text),
        )


@dataclass(frozen=True, slots=True)
class CoverageCounts:
    fact: int
    event: int
    source_window: int
    obligation: int

    def __post_init__(self) -> None:
        for count in (self.fact, self.event, self.source_window, self.obligation):
            _integer(count)

    def to_mapping(self) -> dict[str, int]:
        return {
            "fact": self.fact,
            "event": self.event,
            "source_window": self.source_window,
            "obligation": self.obligation,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoverageCounts:
        item = _closed(value, _KINDS)
        return cls(*(_integer(item[kind]) for kind in _KINDS))


@dataclass(frozen=True, slots=True)
class CoverageLedger:
    ledger_id: str
    input_binding_sha256: str
    draft_sha256: str
    coverage_policy_sha256: str
    windows: tuple[CoverageWindow, ...]
    rows: tuple[CoverageRow, ...]
    taint_seeds: tuple[TaintSeed, ...]
    input_counts: CoverageCounts

    def __post_init__(self) -> None:
        _text(self.ledger_id)
        for value in (self.input_binding_sha256, self.draft_sha256, self.coverage_policy_sha256):
            _hash(value)
        windows, rows, seeds = (
            _tuple(self.windows, CoverageWindow),
            _tuple(self.rows, CoverageRow),
            _tuple(self.taint_seeds, TaintSeed),
        )
        if type(self.input_counts) is not CoverageCounts:  # noqa: E721
            raise LedgerModelError("input counts must be exact CoverageCounts")
        window_ids = {window.window_id for window in windows}
        by_seed = {seed.seed_id: seed for seed in seeds}
        if (
            len(window_ids) != len(windows)
            or len(by_seed) != len(seeds)
            or len({row.coverage_id for row in rows}) != len(rows)
        ):
            raise LedgerModelError("ledger object IDs must be unique within their kind")
        if len({window.source_window_ref for window in windows}) != len(windows):
            raise LedgerModelError("source windows cannot have duplicate coverage objects")
        if len({row.unit_ref for row in rows}) != len(rows):
            raise LedgerModelError("each coverage unit must have exactly one row")
        row_windows = {
            row.unit_ref.window_id
            for row in rows
            if isinstance(row.unit_ref, LocalCoverageWindowRef)
        }
        if row_windows != window_ids:
            raise LedgerModelError("coverage windows and local rows must match exactly")
        if self.actual_counts != self.input_counts:
            raise LedgerModelError("ledger row counts differ from declared input counts")
        assigned_seeds: set[str] = set()
        for row in rows:
            if row.taint_seed_id is None:
                continue
            if row.taint_seed_id not in by_seed or row.taint_seed_id in assigned_seeds:
                raise LedgerModelError("each unresolved row must own one exclusive existing seed")
            assigned_seeds.add(row.taint_seed_id)
            seed = by_seed[row.taint_seed_id]
            rooted = (
                row.unit_ref.window_id in seed.root_window_ids
                if isinstance(row.unit_ref, LocalCoverageWindowRef)
                else row.unit_ref in seed.root_refs
            )
            if not rooted:
                raise LedgerModelError("row seed must retain its owning coverage unit as a root")
        if assigned_seeds != set(by_seed):
            raise LedgerModelError("ledger cannot contain orphan taint seeds")
        for seed in seeds:
            if not set((*seed.root_window_ids, *seed.frontier_window_ids)) <= window_ids:
                raise LedgerModelError("seed window selectors must resolve inside this ledger")
        object.__setattr__(self, "windows", tuple(sorted(windows, key=lambda item: item.window_id)))
        object.__setattr__(self, "rows", tuple(sorted(rows, key=lambda item: item.coverage_id)))
        object.__setattr__(self, "taint_seeds", tuple(sorted(seeds, key=lambda item: item.seed_id)))

    @property
    def actual_counts(self) -> CoverageCounts:
        return CoverageCounts(*(sum(row.unit_type == kind for row in self.rows) for kind in _KINDS))

    def to_mapping(self) -> dict[str, object]:
        actual, inputs = self.actual_counts.to_mapping(), self.input_counts.to_mapping()
        return {
            "ledger_id": self.ledger_id,
            "input_binding_sha256": self.input_binding_sha256,
            "draft_sha256": self.draft_sha256,
            "coverage_policy_sha256": self.coverage_policy_sha256,
            "windows": [window.to_mapping() for window in self.windows],
            "rows": [row.to_mapping() for row in self.rows],
            "taint_seeds": [seed.to_mapping() for seed in self.taint_seeds],
            "conservation": {
                kind: {"input_count": inputs[kind], "ledger_count": actual[kind]} for kind in _KINDS
            },
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoverageLedger:
        item = _closed(
            value,
            (
                "ledger_id",
                "input_binding_sha256",
                "draft_sha256",
                "coverage_policy_sha256",
                "windows",
                "rows",
                "taint_seeds",
                "conservation",
            ),
        )
        conservation = _closed(item["conservation"], _KINDS)
        inputs: dict[str, int] = {}
        actual: dict[str, int] = {}
        for kind in _KINDS:
            counts = _closed(conservation[kind], ("input_count", "ledger_count"))
            inputs[kind], actual[kind] = (
                _integer(counts["input_count"]),
                _integer(counts["ledger_count"]),
            )
        result = cls(
            _text(item["ledger_id"]),
            _hash(item["input_binding_sha256"]),
            _hash(item["draft_sha256"]),
            _hash(item["coverage_policy_sha256"]),
            _array(item["windows"], CoverageWindow.from_mapping),
            _array(item["rows"], CoverageRow.from_mapping),
            _array(item["taint_seeds"], TaintSeed.from_mapping),
            CoverageCounts.from_mapping(inputs),
        )
        if result.actual_counts.to_mapping() != actual:
            raise LedgerModelError("serialized ledger counts differ from actual rows")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
