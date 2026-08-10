"""pipeline/contracts/ — Multi-source conflict resolution layer.

This package provides field classification and deterministic merge logic
for reconciling data from multiple sources (API, LLM, VLM) without requiring
LLM calls for the merge step itself.

Modules
-------
field_registry
    Classifies every field from the 10 autocut tables into 4 categories:
    measurable, author_intent, api_unique, video_verifiable.
merge_operator
    Zero-LLM deterministic merge operator that produces canonical records,
    full provenance traces, and structured conflict lists.
"""

from __future__ import annotations