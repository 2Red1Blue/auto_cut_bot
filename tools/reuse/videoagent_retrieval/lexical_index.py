"""Deterministic lexical retrieval baseline (R2A).

A BM25-style index over projected documents. The index is a derived cache:
fully rebuildable, bound by RetrievalIndexManifest/v1, and every hit must be
re-verified against the source object by the caller — a hit adds no facts.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from tools.reuse.models import json_sha256
from tools.reuse.videoagent_retrieval.projection import IndexDocument

INDEX_MANIFEST_SCHEMA = "RetrievalIndexManifest/v1"
HIT_SCHEMA = "RetrievalHit/v1"

_TOKEN_RE = re.compile(r"[\w一-鿿]+")

K1 = 1.5
B = 0.75


@dataclass(frozen=True)
class RetrievalIndexManifest:
    document_refs: tuple[str, ...]
    document_hashes: tuple[str, ...]
    text_projection_version: str
    algorithm: str
    parameters: dict[str, float]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": INDEX_MANIFEST_SCHEMA,
            "document_refs": list(self.document_refs),
            "document_hashes": list(self.document_hashes),
            "text_projection_version": self.text_projection_version,
            "algorithm": self.algorithm,
            "parameters": self.parameters,
        }

    @property
    def manifest_hash(self) -> str:
        return json_sha256(self.to_mapping())


@dataclass(frozen=True)
class RetrievalHit:
    query_id: str
    rank: int
    object_ref: str
    score: float
    support: str  # coarse support note (matched terms), not new facts
    reason: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": HIT_SCHEMA,
            "query_id": self.query_id,
            "rank": self.rank,
            "object_ref": self.object_ref,
            "score": self.score,
            "support": self.support,
            "reason": self.reason,
        }


class LexicalIndex:
    """BM25 over whitespace/CJK-tokenized document text. Deterministic."""

    ALGORITHM = "bm25-lexical-v1"

    def __init__(self, documents: list[IndexDocument]) -> None:
        if not documents:
            raise ValueError("lexical index needs at least one document")
        seen_refs: set[str] = set()
        self._docs = documents
        self._doc_terms: list[dict[str, int]] = []
        for doc in documents:
            if doc.object_ref in seen_refs:
                raise ValueError(f"duplicate document ref: {doc.object_ref}")
            seen_refs.add(doc.object_ref)
            self._doc_terms.append(_term_counts(doc.text))
        self._avg_len = (
            sum(sum(tc.values()) for tc in self._doc_terms) / len(self._doc_terms) or 1.0
        )
        self._df: dict[str, int] = {}
        for tc in self._doc_terms:
            for term in tc:
                self._df[term] = self._df.get(term, 0) + 1
        self._manifest = RetrievalIndexManifest(
            document_refs=tuple(d.object_ref for d in documents),
            document_hashes=tuple(d.content_hash for d in documents),
            text_projection_version="retrieval-text-projection-v1",
            algorithm=self.ALGORITHM,
            parameters={"k1": K1, "b": B},
        )

    @property
    def manifest(self) -> RetrievalIndexManifest:
        return self._manifest

    def search(self, query_id: str, query_text: str, top_k: int) -> list[RetrievalHit]:
        """Rank documents; hits carry matched terms as coarse support only."""
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        q_terms = _term_counts(query_text)
        if not q_terms:
            return []
        n = len(self._docs)
        scores: list[tuple[float, int, list[str]]] = []
        for idx, tc in enumerate(self._doc_terms):
            doc_len = sum(tc.values())
            score = 0.0
            matched: list[str] = []
            for term, q_count in q_terms.items():
                if term not in tc:
                    continue
                matched.append(term)
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                tf = tc[term]
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * doc_len / self._avg_len))
                score += idf * norm * q_count
            if score > 0:
                scores.append((score, idx, matched))
        scores.sort(key=lambda item: (-item[0], self._docs[item[1]].object_ref))
        hits: list[RetrievalHit] = []
        for rank, (score, idx, matched) in enumerate(scores[:top_k], start=1):
            doc = self._docs[idx]
            hits.append(RetrievalHit(
                query_id=query_id, rank=rank, object_ref=doc.object_ref,
                score=round(score, 6),
                support=f"matched terms: {', '.join(sorted(matched)[:8])}",
                reason=f"{doc.kind} document matches lexical query terms",
            ))
        return hits


def verify_hit_source(hit: RetrievalHit, source_payload_hash: str,
                      manifest: RetrievalIndexManifest) -> bool:
    """Re-read verification: the hit's object must still hash to what the
    manifest bound at build time. Callers MUST run this before using a hit —
    a stale index can never silently contribute facts."""
    try:
        position = manifest.document_refs.index(hit.object_ref)
    except ValueError:
        return False
    return manifest.document_hashes[position] == source_payload_hash


def _term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _TOKEN_RE.findall(text):
        token = token.lower()
        if not token:
            continue
        # CJK: index bigrams so Chinese text retrieves without a segmenter
        if re.fullmatch(r"[一-鿿]+", token):
            if len(token) == 1:
                counts[token] = counts.get(token, 0) + 1
            else:
                for i in range(len(token) - 1):
                    gram = token[i:i + 2]
                    counts[gram] = counts.get(gram, 0) + 1
        else:
            counts[token] = counts.get(token, 0) + 1
    return counts


def manifest_fingerprint(manifest: RetrievalIndexManifest) -> str:
    return json.dumps(manifest.to_mapping(), sort_keys=True, ensure_ascii=False)
