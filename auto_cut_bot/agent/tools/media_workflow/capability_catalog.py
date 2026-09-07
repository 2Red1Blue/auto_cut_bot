"""First-version capability catalog, program-registered from the runtime inventory.

Every descriptor is derived from an existing Kernel Command / runtime stage —
the catalog never exposes Python functions, SQL or shell to the model.
Capabilities that are not closed in the current HTTP/Kernel runtime are marked
`unavailable` with a concrete reason; nothing here can be flipped available by
a model.
"""

from __future__ import annotations

from auto_cut_bot.agent.tools.media_workflow.contracts import (
    CapabilityCatalog,
    CapabilityDescriptor,
)

CATALOG_VERSION = "capability-catalog-v1"


def _d(
    capability_id: str, *, version: str = "v1", inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (), depends_on: tuple[str, ...] = (),
    provider: bool = False, parallel: str | None = "episode",
    recovery: str = "resume_same_key", status: str = "available",
    reason: str | None = None,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, version=version,
        input_artifact_types=inputs, output_artifact_types=outputs,
        depends_on=depends_on, provider_calling=provider,
        parallel_dimension=parallel, recovery=recovery,
        status=status, unavailable_reason=reason,
    )


def build_default_catalog() -> CapabilityCatalog:
    """Inventory-registered capabilities (see runtime/command inventory 2026-09-07)."""
    return CapabilityCatalog(descriptors=(
        _d("run_inspect", parallel=None, recovery="local_reprocess",
           inputs=(), outputs=("run_snapshot",)),
        _d("source_prepare", inputs=("source_inputs",), outputs=("source_evidence",)),
        _d("media_preflight", inputs=("media_files",), outputs=("media_evidence",),
           depends_on=("source_prepare",), provider=True),
        _d("vlm_evidence", inputs=("media_evidence",), outputs=("vlm_pack_set",),
           depends_on=("media_preflight",), provider=True),
        _d("stage1_narrative_graph", inputs=("vlm_pack_set",), outputs=("narrative_graph",),
           depends_on=("vlm_evidence",), provider=True),
        _d("stage2_story_portfolio", inputs=("narrative_graph",), outputs=("story_portfolio",),
           depends_on=("stage1_narrative_graph",), provider=True),
        _d("stage3_editorial_blueprint", inputs=("story_portfolio",),
           outputs=("editorial_blueprint",), depends_on=("stage2_story_portfolio",),
           provider=True),
        _d("recipe_compile", inputs=("editorial_blueprint", "media_evidence"),
           outputs=("recipe",), depends_on=("stage3_editorial_blueprint",),
           parallel=None),
        _d("local_render", status="unavailable",
           reason="LocalRenderOrchestrator is not wired as a durable run stage; "
                  "local rendering is invoked outside the run worker and must be "
                  "started through its own entry point"),
        _d("local_qc", status="unavailable",
           reason="no standalone QC artifact command exists; QC evidence is "
                  "evaluated inside the render orchestrator's promotion gate"),
    ))
