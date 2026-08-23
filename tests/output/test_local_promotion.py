"""Tests for trusted composition of persisted-recipe promotion."""

from __future__ import annotations

from dataclasses import fields

import pytest
from autocut_kernel.output import LocalPromotionError, LocalPromotionRequest, promote_local_output
from autocut_kernel.output.local_promotion import LocalPromotionService
from autocut_kernel.store import PostgresRuntimeStore


def test_request_cannot_substitute_a_store() -> None:
    """Store selection belongs to service construction, never request data."""
    assert "store" not in {field.name for field in fields(LocalPromotionRequest)}


def test_legacy_function_cannot_make_output_visible_without_service() -> None:
    with pytest.raises(LocalPromotionError, match="trusted LocalPromotionService"):
        promote_local_output(object())  # type: ignore[arg-type]


def test_service_rejects_a_postgres_store_subclass() -> None:
    class SubstituteStore(PostgresRuntimeStore):
        pass

    with pytest.raises(LocalPromotionError, match="exact PostgresRuntimeStore"):
        LocalPromotionService(SubstituteStore(lambda: None))  # type: ignore[arg-type]
