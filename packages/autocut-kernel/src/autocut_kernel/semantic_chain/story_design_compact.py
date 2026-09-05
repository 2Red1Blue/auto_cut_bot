"""Compact Stage 2 wire: model decisions in, exact domain choices out."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from .member_refs import SemanticObjectRef
from .narrative_models import ObligationAttributes
from .story_design_compact_context import (
    StoryDesignCompactContext,
    build_story_design_compact_context,
)
from .story_design_compact_models import ProposalDraftSetV2, ProposalDraftV2
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
             "story_design_draft.py", "story_design_models.py", "story_design_validation.py")
    return canonical_json_hash({
        "prompt_version": COMPACT_PROMPT_VERSION, "wire_schema_version": COMPACT_WIRE_SCHEMA_VERSION,
        "projection_version": COMPACT_PROJECTION_VERSION,
        "implementation": {name: sha256_bytes((directory / name).read_bytes()) for name in names},
    })


def _ordered_refs(refs: set[SemanticObjectRef]) -> tuple[SemanticObjectRef, ...]:
    return tuple(sorted(refs, key=lambda ref: canonical_json_bytes(ref.to_mapping())))


def _references(value: object, context: StoryDesignCompactContext, prefix: str) -> tuple[SemanticObjectRef, ...]:
    refs = _array(value, lambda alias: context.resolve(alias, prefix))
    if len(set(refs)) != len(refs):
        raise StoryDesignDraftError("COMPACT_DUPLICATE_REFERENCE")
    return refs


def merge_compact_source_constraints(value: object, context: StoryDesignCompactContext) -> SourceConstraints:
    data = _closed(value, ("source_selection", "allowed_source_refs", "forbidden_source_refs"))
    allowed = _references(data["allowed_source_refs"], context, "s")
    forbidden = _references(data["forbidden_source_refs"], context, "s")
    selection = data["source_selection"]
    if selection == "all_granted" and not allowed:
        model_sources = set(context.granted_sources)
    elif selection == "subset" and allowed:
        model_sources = set(allowed)
    else:
        raise StoryDesignDraftError("COMPACT_SOURCE_SELECTION_INVALID")
    granted = set(context.granted_sources)
    job = context.job_policy.source_constraints
    # Historical empty Job allowlists mean unrestricted, not the empty set.
    job_sources = set(job.allowed_source_refs) if job.allowed_source_refs else granted
    denied = set(job.forbidden_source_refs) | set(forbidden)
    effective = (granted & job_sources & model_sources) - denied
    if not effective:
        # SourceConstraints((), (), ...) would silently turn this into an
        # unrestricted allowlist. Reject explicitly instead of broadening it.
        raise StoryDesignDraftError("COMPACT_MATERIAL_INFEASIBLE")
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
    value = _bounded_value(raw, policy)
    _compact_limits(value, policy)
    root = _closed(value, ("schema_version", "proposals"))
    if root["schema_version"] != COMPACT_WIRE_SCHEMA_VERSION:
        raise StoryDesignDraftError("unsupported compact model wire schema")
    items = _array(root["proposals"], lambda row: _closed(row, _PROPOSAL_FIELDS))
    nodes = {node.node_id: node for node in context.graph.nodes}
    profiles = sorted(context.story_policy.editing_profiles, key=lambda row: canonical_json_bytes(row.to_mapping()))
    profile_refs = {f"style{index}": profile for index, profile in enumerate(profiles, 1)}
    proposals = []
    for ordinal, item in enumerate(items, 1):
        proposal_id = canonical_json_hash({"strategy": COMPACT_PROJECTION_VERSION,
                                          "input_binding_sha256": context.input_binding_sha256,
                                          "proposal_ordinal": ordinal})
        obligations = _references(item["obligation_refs"], context, "o")
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
        for index, row in enumerate(_array(item["material_requirements"], lambda row: _closed(row, _REQUIREMENT_FIELDS)), 1):
            additional = _array(row["additional_checks"], PhysicalRequirement.from_mapping)
            if len(set(additional)) != len(additional):
                raise StoryDesignDraftError("compact physical checks contain duplicates")
            physical = tuple(sorted(set(additional) | set(context.story_policy.required_physical_requirements),
                                    key=lambda check: check.requirement_kind))
            requirements.append(MaterialRequirement(
                canonical_json_hash({"proposal_id": proposal_id, "requirement_ordinal": index}),
                context.resolve(row["obligation_ref"], "o"), _positive(row["minimum_usable_seconds"]),
                physical, merge_compact_source_constraints(row["source_constraints"], context),
            ))
        profile_alias = _text(item["editing_profile_ref"])
        if profile_alias not in profile_refs:
            raise StoryDesignDraftError("COMPACT_EDITING_PROFILE_NOT_FOUND")
        proposals.append(ProposalDraftV2(
            proposal_id, _text(item["title"]), _text(item["narrative_claim"]),
            _references(item["thread_refs"], context, "t"), obligations, _ordered_refs(facts),
            _references(item["key_subject_refs"], context, "p"), _array(item["genre_tags"], _text),
            profile_refs[profile_alias], IntegerRange.from_mapping(item["target_duration_seconds"]),
            _text(item["teaser_strategy"]), _text(item["audience_hook"]), tuple(requirements),
        ))
    result = ProposalDraftSetV2(context.input_binding_sha256, tuple(proposals))
    # Bounds include program expansion: compact spelling cannot bypass domain
    # fact/reference/text ceilings. No truncation of selected obligations.
    expanded = result.to_mapping()
    _check_limits(expanded, policy)
    expanded_refs = sum(len(row.narrative_refs) + len(row.source_refs) + len(row.material_requirements)
                        for row in result.proposals)
    if expanded_refs > policy.max_total_references:
        raise StoryDesignDraftError("expanded compact references exceed bound")
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
