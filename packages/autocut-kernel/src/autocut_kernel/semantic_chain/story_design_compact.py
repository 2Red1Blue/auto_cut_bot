"""Compact Stage 2 wire: model decisions in, exact domain choices out."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from .member_refs import SemanticObjectRef
from .narrative_models import ObligationAttributes
from .story_design_compact_context import (
    StoryDesignCompactContext,
    build_story_design_compact_context,
)
from .story_design_compact_models import ProposalDraftSetV2, ProposalDraftV2
from .story_design_compact_errors import CompactDraftError
from .story_design_draft import StoryDesignDraftError, StoryDesignDraftPolicy, _bounded_value, _check_limits
from .story_design_models import (
    PHYSICAL_REQUIREMENT_MODES,
    IntegerRange,
    MaterialRequirement,
    PhysicalRequirement,
    SourceConstraints,
    _array,
    _closed,
    _positive,
    _text,
)

COMPACT_PROMPT_VERSION = "stage2-proposal-compact-v2"
COMPACT_WIRE_SCHEMA_VERSION = "stage2-story-design-compact-v2"
COMPACT_PROJECTION_VERSION = "stage2-compact-domain-projection-v2"
_T = TypeVar("_T")
_PROPOSAL_FIELDS = (
    "title", "narrative_claim", "thread_refs", "obligation_refs", "key_subject_refs",
    "genre_tags", "editing_profile_ref", "target_duration_seconds", "teaser_strategy",
    "audience_hook", "material_requirements",
)
_REQUIREMENT_FIELDS = ("obligation_ref", "minimum_usable_seconds", "additional_checks", "source_constraints")


def compact_contract_sha256() -> str:
    """Exact installed v2 implementation identity; never used for legacy v1."""
    directory = Path(__file__).parent
    names = ("story_design_compact.py", "story_design_compact_context.py", "story_design_compact_models.py",
             "story_design_compact_errors.py",
             "story_design_draft.py", "story_design_models.py", "story_design_validation.py")
    return canonical_json_hash({
        "prompt_version": COMPACT_PROMPT_VERSION, "wire_schema_version": COMPACT_WIRE_SCHEMA_VERSION,
        "projection_version": COMPACT_PROJECTION_VERSION,
        "implementation": {name: sha256_bytes((directory / name).read_bytes()) for name in names},
    })


def _ordered_refs(refs: set[SemanticObjectRef]) -> tuple[SemanticObjectRef, ...]:
    return tuple(sorted(refs, key=lambda ref: canonical_json_bytes(ref.to_mapping())))


def _at(path: str, proposal_index: int | None, call: Callable[[], _T]) -> _T:
    try:
        return call()
    except CompactDraftError as error:
        raise CompactDraftError(error.error_code, json_path=path, proposal_index=proposal_index) from error
    except ValueError as error:
        raise CompactDraftError("COMPACT_FIELD_INVALID", json_path=path, proposal_index=proposal_index) from error


def _references(
    value: object, context: StoryDesignCompactContext, prefix: str, *, path: str = "$", proposal_index: int | None = None,
) -> tuple[SemanticObjectRef, ...]:
    values = _at(path, proposal_index, lambda: _array(value, lambda item: item))
    refs = []
    for index, alias in enumerate(values):
        ref_path = f"{path}[{index}]"
        ref = _at(ref_path, proposal_index, lambda: context.resolve(alias, prefix))
        if ref in refs:
            raise CompactDraftError("COMPACT_DUPLICATE_REFERENCE", json_path=ref_path, proposal_index=proposal_index)
        refs.append(ref)
    return tuple(refs)


def merge_compact_source_constraints(
    value: object, context: StoryDesignCompactContext, *, path: str = "$", proposal_index: int | None = None,
) -> SourceConstraints:
    data = _at(path, proposal_index, lambda: _closed(value, ("source_selection", "allowed_source_refs", "forbidden_source_refs")))
    allowed = _references(data["allowed_source_refs"], context, "s", path=f"{path}.allowed_source_refs", proposal_index=proposal_index)
    forbidden = _references(data["forbidden_source_refs"], context, "s", path=f"{path}.forbidden_source_refs", proposal_index=proposal_index)
    selection = data["source_selection"]
    if selection == "all_granted" and not allowed:
        model_sources = set(context.granted_sources)
    elif selection == "subset" and allowed:
        model_sources = set(allowed)
    else:
        raise CompactDraftError("COMPACT_SOURCE_SELECTION_INVALID", json_path=f"{path}.source_selection", proposal_index=proposal_index)
    granted = set(context.granted_sources)
    job = context.job_policy.source_constraints
    # Historical empty Job allowlists mean unrestricted, not the empty set.
    job_sources = set(job.allowed_source_refs) if job.allowed_source_refs else granted
    denied = set(job.forbidden_source_refs) | set(forbidden)
    effective = (granted & job_sources & model_sources) - denied
    if not effective:
        # SourceConstraints((), (), ...) would silently turn this into an
        # unrestricted allowlist. Reject explicitly instead of broadening it.
        raise CompactDraftError("COMPACT_MATERIAL_INFEASIBLE", json_path=path, proposal_index=proposal_index)
    return SourceConstraints(_ordered_refs(effective), _ordered_refs(denied), "render_source")


def _compact_limits(value: object, policy: StoryDesignDraftPolicy) -> None:
    _check_limits(value, policy)
    pending = [value]
    references = 0
    ref_arrays = {"thread_refs", "obligation_refs", "key_subject_refs", "allowed_source_refs", "forbidden_source_refs"}
    while pending:
        item = pending.pop()
        if type(item) is list:
            pending.extend(cast(list[object], item))
        elif type(item) is dict:
            for key, child in cast(dict[str, object], item).items():
                if key in ref_arrays and type(child) is list:
                    count = len(cast(list[object], child))
                    references += count
                    if count > policy.max_references_per_field:
                        raise StoryDesignDraftError("compact reference field exceeds bound")
                elif key == "obligation_ref":
                    references += 1
                elif key == "additional_checks" and type(child) is list and len(cast(list[object], child)) > 3:
                    raise StoryDesignDraftError("compact physical check count exceeds bound")
                pending.append(child)
        if references > policy.max_total_references:
            raise StoryDesignDraftError("compact total references exceed bound")


def decode_story_design_compact(
    raw: bytes, *, context: StoryDesignCompactContext, policy: StoryDesignDraftPolicy,
) -> ProposalDraftSetV2:
    if type(context) is not StoryDesignCompactContext or type(policy) is not StoryDesignDraftPolicy:  # noqa: E721
        raise StoryDesignDraftError("compact decoder requires exact typed context and policy")
    if type(raw) is not bytes or not 0 < len(raw) <= policy.max_response_bytes:  # noqa: E721
        raise CompactDraftError("COMPACT_BUDGET_EXCEEDED")
    try:
        value = _bounded_value(raw, policy)
    except ValueError as error:
        raise CompactDraftError("COMPACT_JSON_INVALID") from error
    try:
        _compact_limits(value, policy)
    except ValueError as error:
        raise CompactDraftError("COMPACT_BUDGET_EXCEEDED") from error
    root = _at("$", None, lambda: _closed(value, ("schema_version", "proposals")))
    if root["schema_version"] != COMPACT_WIRE_SCHEMA_VERSION:
        raise CompactDraftError("COMPACT_SCHEMA_UNSUPPORTED", json_path="$.schema_version")
    items = _at("$.proposals", None, lambda: _array(root["proposals"], lambda row: row))
    nodes = {node.node_id: node for node in context.graph.nodes}
    profiles = sorted(context.story_policy.editing_profiles, key=lambda row: canonical_json_bytes(row.to_mapping()))
    profile_refs = {f"style{index}": profile for index, profile in enumerate(profiles, 1)}
    proposals = []
    for ordinal, raw_item in enumerate(items, 1):
        proposal_index = ordinal - 1
        path = f"$.proposals[{proposal_index}]"
        item = _at(path, proposal_index, lambda: _closed(raw_item, _PROPOSAL_FIELDS))
        proposal_id = canonical_json_hash({"strategy": COMPACT_PROJECTION_VERSION,
                                          "input_binding_sha256": context.input_binding_sha256,
                                          "proposal_ordinal": ordinal})
        obligations = _references(item["obligation_refs"], context, "o", path=f"{path}.obligation_refs", proposal_index=proposal_index)
        facts: set[SemanticObjectRef] = set()
        for ref in obligations:
            attributes = nodes[ref.object_id].attributes
            if type(attributes) is not ObligationAttributes:  # noqa: E721
                raise StoryDesignDraftError("compact obligation has wrong Graph type")
            for fact_id in attributes.required_fact_ids:
                if fact_id not in nodes or nodes[fact_id].node_type != "fact":
                    raise StoryDesignDraftError("compact obligation names absent Fact")
                facts.add(SemanticObjectRef(context.graph_owner, "fact", fact_id))
        requirements = []
        material = _at(f"{path}.material_requirements", proposal_index,
                       lambda: _array(item["material_requirements"], lambda row: row))
        for index, raw_row in enumerate(material, 1):
            requirement_path = f"{path}.material_requirements[{index - 1}]"
            row = _at(requirement_path, proposal_index, lambda: _closed(raw_row, _REQUIREMENT_FIELDS))
            additional = _at(f"{requirement_path}.additional_checks", proposal_index,
                             lambda: _array(row["additional_checks"], PhysicalRequirement.from_mapping))
            if len(set(additional)) != len(additional):
                raise CompactDraftError("COMPACT_FIELD_INVALID", json_path=f"{requirement_path}.additional_checks", proposal_index=proposal_index)
            physical = tuple(sorted(set(additional) | set(context.story_policy.required_physical_requirements),
                                    key=lambda check: check.requirement_kind))
            requirements.append(MaterialRequirement(
                canonical_json_hash({"proposal_id": proposal_id, "requirement_ordinal": index}),
                _at(f"{requirement_path}.obligation_ref", proposal_index, lambda: context.resolve(row["obligation_ref"], "o")),
                _at(f"{requirement_path}.minimum_usable_seconds", proposal_index, lambda: _positive(row["minimum_usable_seconds"])),
                physical, merge_compact_source_constraints(row["source_constraints"], context,
                                                           path=f"{requirement_path}.source_constraints", proposal_index=proposal_index),
            ))
        profile_alias = _at(f"{path}.editing_profile_ref", proposal_index, lambda: _text(item["editing_profile_ref"]))
        if profile_alias not in profile_refs:
            raise CompactDraftError("COMPACT_EDITING_PROFILE_NOT_FOUND", json_path=f"{path}.editing_profile_ref", proposal_index=proposal_index)
        proposals.append(ProposalDraftV2(
            proposal_id, _text(item["title"]), _text(item["narrative_claim"]),
            _references(item["thread_refs"], context, "t", path=f"{path}.thread_refs", proposal_index=proposal_index), obligations, _ordered_refs(facts),
            _references(item["key_subject_refs"], context, "p", path=f"{path}.key_subject_refs", proposal_index=proposal_index), _array(item["genre_tags"], _text),
            profile_refs[profile_alias], IntegerRange.from_mapping(item["target_duration_seconds"]),
            _text(item["teaser_strategy"]), _text(item["audience_hook"]), tuple(requirements),
        ))
    result = ProposalDraftSetV2(context.input_binding_sha256, tuple(proposals))
    # Bounds include program expansion: compact spelling cannot bypass domain
    # fact/reference/text ceilings. No truncation of selected obligations.
    expanded = result.to_mapping()
    try:
        _check_limits(expanded, policy)
    except ValueError as error:
        raise CompactDraftError("COMPACT_BUDGET_EXCEEDED") from error
    expanded_refs = sum(len(row.narrative_refs) + len(row.source_refs) + len(row.material_requirements)
                        for row in result.proposals)
    if expanded_refs > policy.max_total_references:
        raise CompactDraftError("COMPACT_BUDGET_EXCEEDED")
    from .story_design_validation import validate_story_proposals

    validate_story_proposals(
        result, graph=context.graph,
        graph_object_refs=tuple(SemanticObjectRef(context.graph_owner, node.node_type, node.node_id)
                                for node in context.graph.nodes),
        source_refs=context.granted_sources, job_policy=context.job_policy, story_policy=context.story_policy,
    )
    return result


def story_design_compact_response_schema(policy: StoryDesignDraftPolicy) -> dict[str, object]:
    if type(policy) is not StoryDesignDraftPolicy:  # noqa: E721
        raise StoryDesignDraftError("compact schema requires an explicit typed policy")

    def obj(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    def text() -> dict[str, object]:
        return {"type": "string", "minLength": 1, "maxLength": policy.max_text_characters, "pattern": r"\S"}

    def alias(prefix: str) -> dict[str, object]:
        return {"type": "string", "pattern": f"^{prefix}[1-9][0-9]*$", "maxLength": policy.max_text_characters}

    def array(item: dict[str, object], maximum: int, minimum: int = 0) -> dict[str, object]:
        return {"type": "array", "items": item, "maxItems": maximum, "minItems": minimum, "uniqueItems": True}

    def refs(prefix: str) -> dict[str, object]:
        return array(alias(prefix), policy.max_references_per_field)

    integer = {"type": "integer", "minimum": 1, "maximum": 2**53 - 1}
    check = {"oneOf": [obj({"requirement_kind": {"const": kind}, "mode": {"const": mode}})
                       for kind, mode in PHYSICAL_REQUIREMENT_MODES]}
    requirement = obj({
        "obligation_ref": alias("o"), "minimum_usable_seconds": integer,
        "additional_checks": array(check, 3),
        "source_constraints": obj({"source_selection": {"enum": ["all_granted", "subset"]},
                                   "allowed_source_refs": refs("s"), "forbidden_source_refs": refs("s")}),
    })
    proposal = obj({
        "title": text(), "narrative_claim": text(), "thread_refs": refs("t"),
        "obligation_refs": refs("o"), "key_subject_refs": refs("p"),
        "genre_tags": array(text(), policy.max_genre_tags, 1), "editing_profile_ref": alias("style"),
        "target_duration_seconds": obj({"min": integer, "max": integer}),
        "teaser_strategy": text(), "audience_hook": text(),
        "material_requirements": {"type": "array", "items": requirement,
                                  "maxItems": policy.max_material_requirements_per_proposal},
    })
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj({
        "schema_version": {"const": COMPACT_WIRE_SCHEMA_VERSION},
        "proposals": {"type": "array", "items": proposal, "maxItems": policy.max_proposals},
    })}


__all__ = (
    "COMPACT_PROMPT_VERSION", "COMPACT_WIRE_SCHEMA_VERSION", "COMPACT_PROJECTION_VERSION",
    "StoryDesignCompactContext", "build_story_design_compact_context", "compact_contract_sha256",
    "decode_story_design_compact", "story_design_compact_response_schema", "merge_compact_source_constraints",
)
