"""Frozen synthetic whole-source baseline before the physical-only extraction.

Captured from the unmodified port at f2c59f35 using the existing fake runner
and speech fixture; no real model or codec execution establishes these values.
"""

from pathlib import Path

from tests.pipeline.test_local_media_preflight import _request, _Runner


def test_physical_extraction_preserves_whole_source_evidence_hash(tmp_path: Path):
    port, request, _speech = _request(tmp_path, _Runner())
    result = port.prepare(
        request, kernel_max_source_bytes=1_000_000, service_max_request_bytes=1_000_000,
    )
    assert result.evidence.canonical_hash == (
        "sha256:a76edd74bf1ded75e8a715872dcf35d1cf306c528886180ccaeaad3139f94040"
    )
