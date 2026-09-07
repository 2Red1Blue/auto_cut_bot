"""Optional VideoAgent/VideoRAG upstream comparison adapter (R2A).

Interface only in this phase: the upstream semantic retrieval stack (CUDA
pytorch + TTS dependencies) is not installed in the experiment environment.
When it is, an implementation maps the SAME frozen documents through the
upstream retriever and returns hits in this contract, so the comparison stays
honest: same visible inputs, same metrics, differences recorded explicitly.
"""

from __future__ import annotations

from typing import Protocol

from tools.reuse.videoagent_retrieval.lexical_index import RetrievalHit
from tools.reuse.videoagent_retrieval.projection import IndexDocument


class UpstreamRetriever(Protocol):
    """Same visible input, same hit contract as the lexical baseline."""

    name: str  # e.g. "VideoAgent-videorag-f207987"
    extra_dependencies: tuple[str, ...]  # recorded in every comparison report

    def search(
        self, query_id: str, query_text: str, top_k: int,
        documents: list[IndexDocument],
    ) -> list[RetrievalHit]: ...


class UpstreamUnavailable:
    """Honest placeholder: the upstream retriever was never executed."""

    name = "unavailable"
    extra_dependencies: tuple[str, ...] = ()

    def search(self, query_id: str, query_text: str, top_k: int,
               documents: list[IndexDocument]) -> list[RetrievalHit]:
        raise NotImplementedError(
            "VideoAgent semantic retrieval is not installed; without it no "
            "upstream-vs-baseline recall comparison may be claimed"
        )
