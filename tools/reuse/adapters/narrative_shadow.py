"""NarratoAI shadow A/B producer adapter (tools/reuse runner interface).

Runs the R1A shadow experiment: identical compact context, identical Stage 1
inputs, only the target prompt strategy differs between arms.

- baseline arm: frozen compact prompt + compact response schema; raw goes to
  the kernel's own strict decoder unchanged.
- candidate arm: compact prompt + narrative-duty prompt section; response is
  decoded by tools.reuse.narrative_strategy.wire (kernel decoder + duty checks).

No production Store is touched; no VLM/Stage 1 re-run; provider is whatever
the host supplies through the R0 ProviderPort (fake for protocol smoke).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import NarrativeGraph
from autocut_kernel.semantic_chain.story_design_boundary import STAGE2_COMPACT_PROMPT
from autocut_kernel.semantic_chain.story_design_compact import (
    decode_story_design_compact,
    story_design_compact_response_schema,
)
from autocut_kernel.semantic_chain.story_design_compact_context import StoryDesignCompactContext
from autocut_kernel.semantic_chain.story_design_draft import StoryDesignDraftPolicy
from autocut_kernel.semantic_chain.story_design_models import JobPolicy, StoryDesignPolicy

from tools.reuse.fixtures import FixtureManifest
from tools.reuse.models import (
    ErrorCode,
    ExperimentError,
    ExperimentSpec,
    MetricResult,
    Projection,
    ProviderCallRequest,
)
from tools.reuse.narrative_strategy.policy import (
    NARRATIVE_POLICY_VERSION,
    candidate_prompt_section,
    extend_response_schema,
)
from tools.reuse.narrative_strategy.wire import (
    DUTY_FIELD_INVALID,
    NarrativeDutyError,
    decode_narrative_story_draft,
)

ADAPTER_VERSION = "narrative-shadow-1"
FAKE_COMMIT = "builtin:narrative-shadow"
PROJECTION_VERSION = "narrative-shadow-projection-v1"

_SHADOW_PAYLOAD_VERSION = "narrative-shadow-fixture-v1"


def _policy_from_mapping(obj: Any, cls: type, what: str):
    try:
        return cls.from_mapping(obj)
    except Exception as exc:
        raise ExperimentError(ErrorCode.INVALID_SPEC, f"{what}: {exc}") from exc


class NarrativeShadowAdapter:
    """One model call per arm (baseline/candidate) per fixture payload."""

    def __init__(self) -> None:
        self._spec: ExperimentSpec | None = None
        self._fixture: FixtureManifest | None = None
        self._accepted: dict[str, str] = {}
        self._decode_errors: dict[str, list[dict[str, Any]]] = {}

    # -- host hooks ---------------------------------------------------------

    def bind(self, spec: ExperimentSpec, fixture: FixtureManifest) -> None:
        self._spec = spec
        self._fixture = fixture

    def load_shadow_payload(self) -> dict[str, Any]:
        """Read the frozen shadow payload (context + policies) from labels_ref JSON.

        The payload carries everything needed to rebuild the compact context
        deterministically: input binding, alias map, graph, policies. It is the
        R1 stand-in for re-reading committed Stage 1 artifacts from a Store;
        R5 replaces it with the real Store read."""
        assert self._fixture is not None
        labels_ref = self._fixture.labels_ref
        if not labels_ref:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, "narrative shadow fixture requires labels_ref payload"
            )
        path = Path(labels_ref)
        if not path.is_file():
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"labels payload missing: {labels_ref}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- R0 narrow interface --------------------------------------------------

    def validate(self, spec: ExperimentSpec, fixture: FixtureManifest) -> None:
        if spec.producer_commit != FAKE_COMMIT:
            raise ExperimentError(ErrorCode.SOURCE_MISMATCH, f"narrative shadow adapter pins {FAKE_COMMIT}")
        if spec.mode == "live":
            if spec.model_request is None or spec.model_request.provider != "fake":
                raise ExperimentError(ErrorCode.INVALID_SPEC, "live shadow experiment requires provider 'fake'")
        if not fixture.inputs:
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "shadow fixture needs inputs")
        if not fixture.labels_ref:
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "shadow fixture needs labels_ref")

    def build_calls(self, state: dict[str, Any]) -> list[ProviderCallRequest]:
        assert self._spec is not None
        pending = set(state.get("accepted_call_ids", ()))
        variant = self._spec.variant
        assert variant in ("baseline", "candidate")
        call_id = f"shadow-{variant}"
        if call_id in pending:
            return []
        # Request identity binds the strategy content: prompt, schema and context
        # hash change => new request hash => old recordings can never be reused.
        payload = self.load_shadow_payload()
        context = self._build_context(payload)
        draft_policy = _policy_from_mapping(payload["draft_policy"], StoryDesignDraftPolicy, "draft_policy")
        if variant == "baseline":
            prompt = STAGE2_COMPACT_PROMPT
            schema = story_design_compact_response_schema(draft_policy)
        else:
            prompt = STAGE2_COMPACT_PROMPT + candidate_prompt_section()
            schema = extend_response_schema(story_design_compact_response_schema(draft_policy))
        return [
            ProviderCallRequest(
                call_id=call_id,
                provider=self._spec.model_request.provider if self._spec.model_request else "fake",
                model=self._spec.model_request.model if self._spec.model_request else "fake-model",
                payload={
                    "task": "stage2_shadow_draft",
                    "variant": variant,
                    "policy_version": NARRATIVE_POLICY_VERSION if variant == "candidate" else "frozen-compact",
                    "seed": self._spec.seed,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "response_schema_sha256": hashlib.sha256(
                        json.dumps(schema, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest(),
                    "context_sha256": context.canonical_hash,
                },
            )
        ]

    def accept_response(self, call_id: str, raw: str) -> None:
        self._accepted[call_id] = raw

    def finalize(self) -> None:
        expected = self.build_calls({"accepted_call_ids": tuple(self._accepted)})
        if expected:
            raise ExperimentError(
                ErrorCode.PROJECTION_REJECTED,
                f"shadow adapter incomplete, missing calls: {[c.call_id for c in expected]}",
            )
        variant = self._spec.variant if self._spec else "baseline"
        call_id = f"shadow-{variant}"
        if call_id not in self._accepted:
            raise ExperimentError(ErrorCode.PROJECTION_REJECTED, f"no accepted response for {call_id}")
        self._decode(call_id)

    def project(self, raw_outputs: dict[str, str]) -> Projection:
        assert self._spec is not None
        variant = self._spec.variant
        call_id = f"shadow-{variant}"
        raw = raw_outputs.get(call_id)
        if raw is None:
            raise ExperimentError(ErrorCode.PROJECTION_REJECTED, f"missing raw for {call_id}")
        draft, errors, duties = self._decode(call_id)
        items: list[dict[str, Any]] = []
        if draft is not None:
            items.append({
                "variant": variant,
                "proposal_count": len(draft.proposals),
                "proposal_ids": [p.proposal_id for p in draft.proposals],
                "duty_count": duties,
                "decode_ok": True,
            })
        else:
            items.append({"variant": variant, "decode_ok": False, "duty_count": 0})
        unmapped = [{"kind": "decode_errors", "entries": errors}] if errors else []
        return Projection(
            producer_id="narrative-shadow", projection_version=PROJECTION_VERSION,
            items=items, unmapped=unmapped,
        )

    # -- decoding -------------------------------------------------------------

    def _decode(self, call_id: str) -> tuple[Any, list[dict[str, Any]], int]:
        """Decode the accepted raw once and memoize. Returns (draft, errors, duty_count)."""
        if call_id in self._decode_errors:
            cached_errors = self._decode_errors[call_id]
            return None, cached_errors, 0  # pragma: no cover - cache hit implies failure
        assert self._spec is not None
        raw = self._accepted[call_id]
        if isinstance(raw, str):
            raw = raw.encode("utf-8")  # kernel decoder requires bytes
        variant = self._spec.variant
        payload = self.load_shadow_payload()
        context = self._build_context(payload)
        draft_policy = _policy_from_mapping(payload["draft_policy"], StoryDesignDraftPolicy, "draft_policy")
        try:
            if variant == "baseline":
                draft = decode_story_design_compact(raw, context=context, policy=draft_policy)
                return draft, [], 0
            narrative = decode_narrative_story_draft(raw, context=context, policy=draft_policy)
            return narrative.base, list(narrative.order_notes), narrative.duty_count
        except NarrativeDutyError as exc:
            self._decode_errors[call_id] = [exc.to_diagnostic()]
            return None, self._decode_errors[call_id], 0
        except Exception as exc:  # kernel strict decode failure (CompactDraftError etc.)
            diagnostic = {
                "code": DUTY_FIELD_INVALID if variant == "candidate" else "COMPACT_DECODE_FAILED",
                "message": str(exc)[:500],
                "proposal_index": None,
                "json_path": None,
            }
            self._decode_errors[call_id] = [diagnostic]
            return None, self._decode_errors[call_id], 0

    def _build_context(self, payload: dict[str, Any]) -> StoryDesignCompactContext:
        """Rebuild the compact context from the frozen payload (deterministic)."""
        try:
            return StoryDesignCompactContext(
                input_binding_sha256=payload["input_binding_sha256"],
                aliases=tuple(
                    (entry["alias"],
                     SemanticObjectRef.from_mapping(entry["reference"]))
                    for entry in payload["aliases"]
                ),
                graph=NarrativeGraph.from_mapping(payload["graph"]),
                graph_owner=SemanticMemberIdentity.from_mapping(payload["graph_owner"]),
                granted_sources=tuple(
                    SemanticObjectRef.from_mapping(ref) for ref in payload["granted_sources"]
                ),
                job_policy=_policy_from_mapping(payload["job_policy"], JobPolicy, "job_policy"),
                story_policy=_policy_from_mapping(payload["story_policy"], StoryDesignPolicy, "story_policy"),
                candidate_policy=_policy_from_mapping(
                    payload["candidate_policy"], CandidateCatalogPolicy, "candidate_policy"
                ),
                model_view_json=payload["model_view_json"],
            )
        except ExperimentError:
            raise
        except KeyError as exc:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"shadow payload missing key: {exc}"
            ) from exc
        except Exception as exc:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"shadow payload invalid: {exc}"
            ) from exc

    # -- metrics --------------------------------------------------------------

    def compute_metrics(self, projection: Projection, manifest: FixtureManifest) -> list[MetricResult]:
        assert self._spec is not None
        variant = self._spec.variant
        item = projection.items[0] if projection.items else {}
        denom = 1.0
        num = 1.0 if item.get("decode_ok") else 0.0
        structural_pass = MetricResult(
            f"decode_pass_{variant}", num / denom, num=num, denom=denom,
        )
        if not item.get("decode_ok"):
            return [structural_pass]
        duty_count = float(item.get("duty_count", 0))
        # duty coverage: duties attached per decoded proposal (denominator = proposals)
        proposals = float(item.get("proposal_count", 0) or 0)
        if proposals == 0:
            return [structural_pass, MetricResult(
                f"duty_coverage_{variant}", None, missing_reason="no proposals decoded")]
        return [
            structural_pass,
            MetricResult(
                f"duty_coverage_{variant}", duty_count / proposals,
                num=duty_count, denom=proposals,
            ),
        ]
