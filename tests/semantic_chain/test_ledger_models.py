"""Synthetic Ledger values, not a committed-input or admission fixture."""

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace

import pytest
from autocut_kernel.semantic_chain.ledger_models import (
    CoverageCounts,
    CoverageLedger,
    CoverageRow,
    CoverageWindow,
    LedgerModelError,
    LocalCoverageWindowRef,
    TaintSeed,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.store import ArtifactScope


def _hash(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _ref(artifact, kind, identifier):
    owner = SemanticMemberIdentity(
        artifact, artifact, 1, ArtifactScope("test", "job", "ledger"), _hash(artifact)
    )
    return SemanticObjectRef(owner, kind, identifier)


FACT = _ref("narrative_graph", "fact", "事实")
EVENT = _ref("event_card_set", "event", "event")
OBLIGATION = _ref("narrative_graph", "obligation", "obligation")
BEAT = _ref("narrative_graph", "beat", "beat")
DIGEST = _ref("episode_digest_set", "episode_digest", "episode")
RAW = _ref("vlm_semantic_pack", "vlm_fact", _hash("raw"))
SOURCE = _ref("whole_series_source_manifest", "source", "source")
SOURCE_WINDOW = _ref("whole_series_source_manifest", "source_window", _hash("window"))
DIAGNOSTIC = _ref("evidence_diagnostics", "diagnostic", "diagnostic")
CONFLICT = _ref("conflict_diagnostics", "diagnostic", "conflict")
WINDOW = CoverageWindow("window", SOURCE_WINDOW, SOURCE, (FACT,), (EVENT,))
LOCAL = LocalCoverageWindowRef("window")


def _row(kind="fact", status="resolved", disposition="narrative"):
    target = {"fact": FACT, "event": EVENT, "obligation": OBLIGATION, "source_window": LOCAL}[kind]
    return CoverageRow(
        "coverage-" + kind,
        kind,
        target,
        status,
        disposition,
        (DIGEST,) if kind == "source_window" else (BEAT,),
        (RAW,),
        () if status == "resolved" else (DIAGNOSTIC,),
        None if status == "resolved" else "seed-" + kind,
    )


def _seed(kind="fact"):
    target = {"fact": FACT, "event": EVENT, "obligation": OBLIGATION}[kind]
    return TaintSeed("seed-" + kind, (target,), (), (), (), ("low_confidence",))


def _ledger():
    rows = (
        _row("fact", "unresolved", "unassigned"),
        _row("event"),
        _row("source_window"),
        _row("obligation", "conflicted", "supporting"),
    )
    return CoverageLedger(
        "ledger",
        _hash("inputs"),
        _hash("draft"),
        _hash("policy"),
        (WINDOW,),
        rows,
        (_seed("fact"), _seed("obligation")),
        CoverageCounts(1, 1, 1, 1),
    )


VALUES = (LOCAL, WINDOW, _row(), _seed(), CoverageCounts(1, 1, 1, 1), _ledger())


@pytest.mark.parametrize("value", VALUES)
def test_closed_roundtrip_and_deep_immutable_values(value):
    wire = value.to_mapping()
    assert type(value).from_mapping(json.loads(json.dumps(wire))) == value
    before = value.to_mapping()
    for key in wire:
        wire[key] = None
    assert value.to_mapping() == before
    for field in fields(value):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field.name, None)


@pytest.mark.parametrize("value", VALUES)
def test_each_wire_field_required_and_unknown_fields_rejected(value):
    wire = value.to_mapping()
    for key in wire:
        with pytest.raises(LedgerModelError):
            type(value).from_mapping({name: item for name, item in wire.items() if name != key})
    with pytest.raises(LedgerModelError):
        type(value).from_mapping({**wire, "accepted": True})


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("bad", [None, [], (), "object", 1, True])
def test_non_object_wire(value, bad):
    with pytest.raises(LedgerModelError):
        type(value).from_mapping(bad)


def test_fresh_mapping_recursively_detaches_refs():
    ledger = _ledger()
    wire = ledger.to_mapping()
    wire["rows"][0]["unit_ref"]["member_ref"]["scope"]["key"] = "changed"
    wire["windows"][0]["fact_refs"].clear()
    wire["taint_seeds"][0]["reason_codes"].append("changed")
    wire["conservation"]["fact"]["input_count"] = 99
    assert ledger == _ledger()
    assert ledger.to_mapping() == _ledger().to_mapping()


def test_canonical_order_and_independent_hash_oracle():
    ledger = _ledger()
    reordered = replace(
        ledger, rows=tuple(reversed(ledger.rows)), taint_seeds=tuple(reversed(ledger.taint_seeds))
    )
    assert reordered == ledger
    raw = json.dumps(
        ledger.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert ledger.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert (
        replace(ledger, draft_sha256=_hash("different draft")).canonical_hash
        != ledger.canonical_hash
    )
    seed = replace(
        _seed(), root_refs=(OBLIGATION, FACT), reason_codes=("unassigned", "low_confidence")
    )
    assert seed == replace(
        seed,
        root_refs=tuple(reversed(seed.root_refs)),
        reason_codes=("low_confidence", "unassigned"),
    )


@pytest.mark.parametrize("status", ["resolved", "unresolved", "conflicted", "unknown"])
@pytest.mark.parametrize(
    "disposition", ["narrative", "supporting", "unassigned", "intentionally_excluded", "unknown"]
)
def test_exact_orthogonal_matrix(status, disposition):
    allowed = {
        ("resolved", "narrative"),
        ("resolved", "supporting"),
        ("unresolved", "unassigned"),
        ("conflicted", "narrative"),
        ("conflicted", "supporting"),
        ("conflicted", "unassigned"),
    }
    if (status, disposition) in allowed:
        assert CoverageRow.from_mapping(_row(status=status, disposition=disposition).to_mapping())
    else:
        with pytest.raises(LedgerModelError):
            _row(status=status, disposition=disposition)


@pytest.mark.parametrize("bad", ["", " \n", "\ud800", b"text", 1, 1.5, True, None])
def test_strict_text_direct_and_wire(bad):
    with pytest.raises(LedgerModelError):
        LocalCoverageWindowRef(bad)
    with pytest.raises(LedgerModelError):
        LocalCoverageWindowRef.from_mapping(
            {"reference_type": "local_coverage_window", "window_id": bad}
        )
    with pytest.raises(LedgerModelError):
        replace(_row(), coverage_id=bad)
    with pytest.raises(LedgerModelError):
        replace(_seed(), reason_codes=(bad,))


@pytest.mark.parametrize("field", ["fact", "event", "source_window", "obligation"])
@pytest.mark.parametrize("bad", [-1, 2**53, True, False, 1.0, float("nan"), "1", None])
def test_counts_are_exact_nonnegative_safe_integers(field, bad):
    with pytest.raises(LedgerModelError):
        replace(CoverageCounts(0, 0, 0, 0), **{field: bad})
    with pytest.raises(LedgerModelError):
        CoverageCounts.from_mapping({**CoverageCounts(0, 0, 0, 0).to_mapping(), field: bad})


def test_empty_value_is_not_admission_or_real_input_conservation():
    ledger = CoverageLedger(
        "empty",
        _hash("inputs"),
        _hash("draft"),
        _hash("policy"),
        (),
        (),
        (),
        CoverageCounts(0, 0, 0, 0),
    )
    assert CoverageLedger.from_mapping(ledger.to_mapping()) == ledger
    assert CoverageCounts(2**53 - 1, 0, 0, 0).fact == 2**53 - 1
    assert "accepted" not in ledger.to_mapping()


@pytest.mark.parametrize(
    "field", ["input_binding_sha256", "draft_sha256", "coverage_policy_sha256"]
)
@pytest.mark.parametrize(
    "bad", ["", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "a" * 64, True, None]
)
def test_binding_hashes_strict(field, bad):
    with pytest.raises(LedgerModelError):
        replace(_ledger(), **{field: bad})


@pytest.mark.parametrize("field", ["assignment_refs", "evidence_refs", "diagnostic_refs"])
def test_rows_require_actual_tuple_and_unique_exact_refs(field):
    value = {"assignment_refs": BEAT, "evidence_refs": RAW, "diagnostic_refs": DIAGNOSTIC}[field]
    for bad in ([value], (value.to_mapping(),), (value, value), None):
        with pytest.raises(LedgerModelError):
            replace(_row(), **{field: bad})
    wire = _row().to_mapping()
    wire[field] = ()
    with pytest.raises(LedgerModelError):
        CoverageRow.from_mapping(wire)


@pytest.mark.parametrize("kind", ["fact", "event", "obligation", "source_window"])
@pytest.mark.parametrize("wrong", [RAW, SOURCE, BEAT, "bare-id", None])
def test_unit_refs_require_canonical_owner_and_local_windows(kind, wrong):
    with pytest.raises(LedgerModelError):
        replace(_row(kind), unit_ref=wrong)


def test_local_window_wire_not_a_self_hash_and_no_compatibility_union():
    assert LOCAL.to_mapping() == {"reference_type": "local_coverage_window", "window_id": "window"}
    for bad in (
        {"reference_type": "coverage_window", "window_id": "window"},
        {
            "reference_type": "local_coverage_window",
            "window_id": "window",
            "content_hash": _hash("self"),
        },
    ):
        with pytest.raises(LedgerModelError):
            LocalCoverageWindowRef.from_mapping(bad)
    with pytest.raises(LedgerModelError):
        replace(
            _row("source_window"), unit_ref=_ref("coverage_ledger", "coverage_window", "window")
        )


@pytest.mark.parametrize(
    "artifact,kind",
    [
        ("narrative_graph", "event"),
        ("coverage_ledger", "fact"),
        ("dependency_closure_proof", "fact"),
        ("coverage_admission", "fact"),
        ("source_operation_grant", "source"),
    ],
)
def test_late_owner_self_hash_alias_and_grant_are_not_semantic_refs(artifact, kind):
    ref = _ref(artifact, kind, "same-id")
    for field in ("assignment_refs", "evidence_refs"):
        with pytest.raises(LedgerModelError):
            replace(_row(), **{field: (ref,)})
    for field in ("root_refs", "frontier_refs"):
        with pytest.raises(LedgerModelError):
            replace(_seed(), **{field: (ref,)})


def test_canonical_event_in_assignments_seeds_and_evidence():
    row = replace(_row(), assignment_refs=(EVENT,), evidence_refs=(EVENT,))
    assert CoverageRow.from_mapping(row.to_mapping()) == row
    assert TaintSeed.from_mapping(replace(_seed(), root_refs=(EVENT,)).to_mapping()).root_refs == (
        EVENT,
    )


@pytest.mark.parametrize("field", ["source_ref", "source_window_ref"])
def test_source_owner_and_object_type_are_closed(field):
    for bad in (FACT, RAW, _ref("source_manifest", "source", "source"), None):
        with pytest.raises(LedgerModelError):
            replace(WINDOW, **{field: bad})
    original = getattr(WINDOW, field)
    foreign = replace(
        original, member_ref=replace(original.member_ref, content_hash=_hash("foreign"))
    )
    with pytest.raises(LedgerModelError):
        replace(WINDOW, **{field: foreign})


@pytest.mark.parametrize("field,ref", [("fact_refs", FACT), ("event_refs", EVENT)])
def test_window_fact_event_collections(field, ref):
    for bad in ([ref], (ref, ref), (RAW,), (ref.to_mapping(),)):
        with pytest.raises(LedgerModelError):
            replace(WINDOW, **{field: bad})


def test_global_vlm_and_source_window_object_ids_are_hashes():
    with pytest.raises(LedgerModelError):
        replace(WINDOW, source_window_ref=replace(SOURCE_WINDOW, object_id="bare-window"))
    with pytest.raises(LedgerModelError):
        replace(_row(), evidence_refs=(replace(RAW, object_id="bare-fact"),))


def test_seed_diagnostic_requirement_is_orthogonal_to_conflict_assignment():
    for status in ("unresolved", "conflicted"):
        row = _row(status=status, disposition="unassigned")
        for bad in (None, "", True):
            with pytest.raises(LedgerModelError):
                replace(row, taint_seed_id=bad)
        with pytest.raises(LedgerModelError):
            replace(row, diagnostic_refs=())
    with pytest.raises(LedgerModelError):
        replace(_row(), taint_seed_id="seed")
    assert replace(_row(status="conflicted", disposition="narrative"), diagnostic_refs=(CONFLICT,))
    for ref in (
        FACT,
        _ref("evidence_diagnostics", "finding", "finding"),
        _ref("coverage_admission", "diagnostic", "diagnostic"),
    ):
        with pytest.raises(LedgerModelError):
            replace(_row(), diagnostic_refs=(ref,))


@pytest.mark.parametrize(
    "field",
    ["root_refs", "frontier_refs", "root_window_ids", "frontier_window_ids", "reason_codes"],
)
def test_seed_collections_are_unique_tuples(field):
    value = FACT if field.endswith("refs") else "identifier"
    for bad in ([value], (value, value), None):
        with pytest.raises(LedgerModelError):
            replace(_seed(), **{field: bad})


def test_seed_needs_roots_and_reason_but_frontier_can_overlap_roots():
    with pytest.raises(LedgerModelError):
        replace(_seed(), root_refs=())
    with pytest.raises(LedgerModelError):
        replace(_seed(), reason_codes=())
    assert replace(_seed(), frontier_refs=(FACT,))
    window_seed = TaintSeed(
        "seed-source_window", (), ("window",), (), ("window",), ("continuity_missing_context",)
    )
    ledger = _ledger()
    rows = tuple(
        _row("source_window", "unresolved", "unassigned")
        if row.unit_type == "source_window"
        else row
        for row in ledger.rows
    )
    assert replace(ledger, rows=rows, taint_seeds=(*ledger.taint_seeds, window_seed))
    with pytest.raises(LedgerModelError):
        TaintSeed.from_mapping({**window_seed.to_mapping(), "isolation_status": "bounded"})


@pytest.mark.parametrize(
    "reason", ["missing_context", "missing_evidence", "bounded", "accepted", "other"]
)
def test_seed_reason_vocabulary_is_closed_at_both_boundaries(reason):
    with pytest.raises(LedgerModelError, match="reason code"):
        replace(_seed(), reason_codes=(reason,))
    with pytest.raises(LedgerModelError, match="reason code"):
        TaintSeed.from_mapping({**_seed().to_mapping(), "reason_codes": [reason]})


@pytest.mark.parametrize("reason", ["identity_unresolved", "continuity_missing_context"])
@pytest.mark.parametrize("additional", [(), ("low_confidence",)])
def test_unknown_semantics_cannot_have_an_empty_combined_frontier(reason, additional):
    reasons = (reason, *additional)
    with pytest.raises(LedgerModelError, match="explicit frontier"):
        replace(_seed(), reason_codes=reasons)
    with pytest.raises(LedgerModelError, match="explicit frontier"):
        TaintSeed.from_mapping({**_seed().to_mapping(), "reason_codes": list(reasons)})
    for frontier in (
        {"frontier_refs": (FACT,)},
        {"frontier_window_ids": ("window",)},
        {"frontier_refs": (FACT,), "frontier_window_ids": ("window",)},
    ):
        seed = replace(_seed(), reason_codes=reasons, **frontier)
        assert TaintSeed.from_mapping(seed.to_mapping()) == seed
        assert not hasattr(seed, "isolation_status")


@pytest.mark.parametrize(
    "reason", ["unassigned", "summary_evidence_missing", "low_confidence", "continuity_conflict"]
)
def test_other_registered_reasons_can_have_no_frontier_without_claiming_isolation(reason):
    seed = replace(_seed(), reason_codes=(reason,))
    assert TaintSeed.from_mapping(seed.to_mapping()) == seed
    assert not seed.frontier_refs and not seed.frontier_window_ids


@pytest.mark.parametrize("field", ["windows", "rows", "taint_seeds"])
def test_ledger_collection_types_and_duplicate_ids(field):
    ledger = _ledger()
    values = getattr(ledger, field)
    for bad in (list(values), (*values, values[0]), (values[0].to_mapping(),), None):
        with pytest.raises(LedgerModelError):
            replace(ledger, **{field: bad})


def test_exact_unit_identity_not_just_local_object_id():
    ledger = _ledger()
    different_owner = replace(
        FACT, member_ref=replace(FACT.member_ref, content_hash=_hash("foreign graph"))
    )
    extra = replace(_row(), coverage_id="different-owner", unit_ref=different_owner)
    assert replace(ledger, rows=(*ledger.rows, extra), input_counts=CoverageCounts(2, 1, 1, 1))
    with pytest.raises(LedgerModelError):
        replace(
            ledger,
            rows=(*ledger.rows, replace(_row(), coverage_id="duplicate-unit")),
            input_counts=CoverageCounts(2, 1, 1, 1),
        )


def test_seed_ownership_missing_shared_orphan_and_wrong_root():
    ledger = _ledger()
    with pytest.raises(LedgerModelError):
        replace(ledger, taint_seeds=ledger.taint_seeds[:1])
    with pytest.raises(LedgerModelError):
        replace(ledger, taint_seeds=(*ledger.taint_seeds, _seed("event")))
    shared = tuple(
        replace(row, taint_seed_id="seed-fact") if row.unit_type == "obligation" else row
        for row in ledger.rows
    )
    with pytest.raises(LedgerModelError):
        replace(ledger, rows=shared)
    with pytest.raises(LedgerModelError):
        replace(ledger, taint_seeds=(replace(_seed(), root_refs=(EVENT,)), _seed("obligation")))


def test_every_window_has_exactly_one_local_row():
    ledger = _ledger()
    rows = tuple(row for row in ledger.rows if row.unit_type != "source_window")
    with pytest.raises(LedgerModelError):
        replace(ledger, rows=rows, input_counts=CoverageCounts(1, 1, 0, 1))
    with pytest.raises(LedgerModelError):
        replace(ledger, windows=())
    foreign = tuple(
        replace(row, unit_ref=LocalCoverageWindowRef("foreign"))
        if row.unit_type == "source_window"
        else row
        for row in ledger.rows
    )
    with pytest.raises(LedgerModelError):
        replace(ledger, rows=foreign)
    with pytest.raises(LedgerModelError):
        replace(ledger, windows=(*ledger.windows, replace(WINDOW, window_id="alias")))


@pytest.mark.parametrize("field", ["root_window_ids", "frontier_window_ids"])
def test_seed_local_windows_cannot_escape_ledger(field):
    with pytest.raises(LedgerModelError):
        replace(
            _ledger(), taint_seeds=(replace(_seed(), **{field: ("foreign",)}), _seed("obligation"))
        )


def test_declared_and_serialized_conservation_is_recomputed():
    ledger = _ledger()
    assert ledger.actual_counts == ledger.input_counts
    with pytest.raises(LedgerModelError):
        replace(ledger, input_counts=CoverageCounts(2, 1, 1, 1))
    with pytest.raises(LedgerModelError):
        replace(ledger, input_counts={"fact": 1, "event": 1, "source_window": 1, "obligation": 1})
    for kind in ("fact", "event", "source_window", "obligation"):
        for field in ("input_count", "ledger_count"):
            for bad in (0, 2, True, 1.0, "1"):
                wire = ledger.to_mapping()
                wire["conservation"][kind][field] = bad
                with pytest.raises(LedgerModelError):
                    CoverageLedger.from_mapping(wire)
        wire = ledger.to_mapping()
        wire["conservation"][kind]["accepted_count"] = 1
        with pytest.raises(LedgerModelError):
            CoverageLedger.from_mapping(wire)


def test_exact_types_no_subclass_coercion():
    class String(str):
        pass

    class Number(int):
        pass

    class Counts(CoverageCounts):
        pass

    class Target(LocalCoverageWindowRef):
        pass

    for call in (
        lambda: LocalCoverageWindowRef(String("window")),
        lambda: CoverageCounts(Number(1), 0, 0, 0),
        lambda: replace(_ledger(), input_counts=Counts(1, 1, 1, 1)),
        lambda: replace(_row("source_window"), unit_ref=Target("window")),
    ):
        with pytest.raises(LedgerModelError):
            call()
