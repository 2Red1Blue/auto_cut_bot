"""Producer adapters behind the narrow R0 interface.

An adapter implements exactly five methods (plus optional metric helpers):

    validate(spec, fixture)        refuse early on unsupported combos
    build_calls(state)             planned ProviderCallRequest/v1 list
    accept_response(call_id, raw)  record one response into internal state
    finalize()                     adapter-level completeness check
    project(raw_outputs)           deterministic raw -> Projection

Hosts rebuild adapter state after a crash by replaying accept_response over
saved completed calls; adapters therefore must be deterministic and must not
hide state on disk themselves.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from tools.reuse.fixtures import FixtureManifest
from tools.reuse.models import ErrorCode, ExperimentError, ExperimentSpec, Projection


class ProducerAdapter(Protocol):
    def validate(self, spec: ExperimentSpec, fixture: FixtureManifest) -> None: ...

    def build_calls(self, state: dict[str, Any]) -> list[Any]: ...

    def accept_response(self, call_id: str, raw: str) -> None: ...

    def finalize(self) -> None: ...

    def project(self, raw_outputs: dict[str, str]) -> Projection: ...


def load_adapter(adapter_module: str) -> ProducerAdapter:
    """Import and instantiate the adapter class, e.g.
    `tools.reuse.adapters.fake.FakeAdapter` -> class `FakeAdapter` in that module."""
    if not adapter_module.startswith("tools.reuse.adapters."):
        raise ExperimentError(
            ErrorCode.SOURCE_MISMATCH, f"adapter_module must live in tools/reuse/adapters: {adapter_module!r}"
        )
    module_name, class_name = adapter_module.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ExperimentError(ErrorCode.SOURCE_MISMATCH, f"adapter load failed: {exc}") from exc
    return cls()  # type: ignore[no-any-return]
