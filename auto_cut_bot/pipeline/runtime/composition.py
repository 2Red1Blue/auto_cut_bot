"""Fail-closed runtime composition for the PostgreSQL pipeline control plane."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import psycopg

from .models import PipelineRunRequest
from .ports import PipelineRunService
from .postgres import ConnectionFactory, PostgresPipelineRunStore, PostgresPipelineScheduler
from .service import DurablePipelineRunService

PIPELINE_POSTGRES_DSN_ENV = "AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN"
PIPELINE_SOURCE_ROOTS_ENV = "AUTO_CUT_BOT_PIPELINE_SOURCE_ROOTS"
PIPELINE_SOURCE_REFERENCES_ENV = "AUTO_CUT_BOT_PIPELINE_SOURCE_REFERENCES"


class ConfiguredSourceAuthority:
    """Authorize roots by containment and references by exact opaque identity."""

    def __init__(
        self,
        source_roots: tuple[Path, ...],
        source_references: frozenset[str],
    ) -> None:
        self._source_roots = tuple(
            root.expanduser().resolve(strict=False) for root in source_roots
        )
        self._source_references = source_references

    def allows(self, request: PipelineRunRequest) -> bool:
        if request.source_root is not None:
            candidate = Path(request.source_root).expanduser().resolve(strict=False)
            return any(candidate.is_relative_to(root) for root in self._source_roots)
        reference = request.source_reference
        return reference is not None and reference in self._source_references


def compose_pipeline_run_service_from_environment() -> PipelineRunService | None:
    """Build the real service only when DB and source authority are configured."""
    dsn = os.environ.get(PIPELINE_POSTGRES_DSN_ENV, "").strip()
    roots_value = os.environ.get(PIPELINE_SOURCE_ROOTS_ENV, "").strip()
    references_value = os.environ.get(PIPELINE_SOURCE_REFERENCES_ENV, "").strip()
    roots = tuple(Path(value) for value in roots_value.split(os.pathsep) if value.strip())
    references = frozenset(
        value.strip() for value in references_value.split(",") if value.strip()
    )
    if not dsn or (not roots and not references):
        return None

    connection_factory = cast(ConnectionFactory, lambda: psycopg.connect(dsn))
    store = PostgresPipelineRunStore(connection_factory)
    scheduler = PostgresPipelineScheduler(connection_factory)
    return DurablePipelineRunService(
        store,
        scheduler,
        ConfiguredSourceAuthority(roots, references),
    )
