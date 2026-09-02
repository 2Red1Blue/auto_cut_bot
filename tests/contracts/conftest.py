"""Shared source-schema validation policy for contract tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SourceSchemaValidator = Callable[
    [Mapping[str, object], Iterable[Mapping[str, object]]], Draft202012Validator
]


@pytest.fixture
def source_schema_validator() -> SourceSchemaValidator:
    """Build a source-schema validator with mandatory RFC3339 format checks."""

    format_checker = FormatChecker()

    @format_checker.checks("date-time")
    def _calendar_valid_date_time(value: object) -> bool:
        """Reject syntactically valid but impossible calendar dates.

        jsonschema's built-in checker intentionally follows the permissive
        RFC3339 grammar and does not reject dates such as February 30.  The
        v2.1.3 source contract requires an actual UTC timestamp; schema
        patterns still enforce the exact wire shape where applicable.
        """
        if not isinstance(value, str):
            return True
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    def build(
        root_schema: Mapping[str, object],
        dependency_schemas: Iterable[Mapping[str, object]] = (),
    ) -> Draft202012Validator:
        resources = (root_schema, *dependency_schemas)
        for schema in resources:
            if not isinstance(schema.get("$id"), str):
                raise ValueError("source schemas must declare a string $id")
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in resources
        )
        return Draft202012Validator(
            root_schema,
            registry=registry,
            format_checker=format_checker,
        )

    return build
