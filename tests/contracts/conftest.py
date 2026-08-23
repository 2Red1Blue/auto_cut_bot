"""Shared source-schema validation policy for contract tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SourceSchemaValidator = Callable[
    [Mapping[str, object], Iterable[Mapping[str, object]]], Draft202012Validator
]


@pytest.fixture
def source_schema_validator() -> SourceSchemaValidator:
    """Build a source-schema validator with mandatory RFC3339 format checks."""

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
            format_checker=FormatChecker(),
        )

    return build
