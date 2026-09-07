"""Unit tests for closed DTOs and the strict JSON codec."""

from __future__ import annotations

import pytest

from tests.reuse.conftest import make_spec
from tools.reuse.models import (
    ErrorCode,
    ExperimentError,
    ExperimentSpec,
    ProviderCallRequest,
    json_sha256,
    load_json_strict,
)


class TestStrictCodec:
    def test_duplicate_keys_rejected(self) -> None:
        with pytest.raises(ExperimentError) as exc:
            load_json_strict('{"a": 1, "a": 2}', context="x")
        assert exc.value.code == ErrorCode.INVALID_SPEC

    def test_invalid_json_wrapped(self) -> None:
        with pytest.raises(ExperimentError) as exc:
            load_json_strict("{oops}", context="x")
        assert exc.value.code == ErrorCode.INVALID_SPEC


class TestExperimentSpec:
    def test_valid_replay_spec(self, fake_fixture: dict[str, str]) -> None:
        spec = ExperimentSpec.from_dict(make_spec(fake_fixture))
        assert spec.mode == "replay"
        assert spec.model_request is None
        assert spec.spec_hash == spec.spec_hash  # deterministic property

    def test_unknown_field_rejected(self, fake_fixture: dict[str, str]) -> None:
        bad = make_spec(fake_fixture, sneaky_field="x")
        with pytest.raises(ExperimentError) as exc:
            ExperimentSpec.from_dict(bad)
        assert "unknown keys" in exc.value.message

    def test_replay_forbids_model_request(self, fake_fixture: dict[str, str]) -> None:
        bad = make_spec(
            fake_fixture,
            model_request={"provider": "fake", "model": "m", "prompt_id": "p", "schema_id": "s"},
        )
        with pytest.raises(ExperimentError):
            ExperimentSpec.from_dict(bad)

    def test_live_requires_budget_and_model_request(self, fake_fixture: dict[str, str]) -> None:
        base = make_spec(fake_fixture, mode="live")
        with pytest.raises(ExperimentError) as exc:
            ExperimentSpec.from_dict(base)
        assert "model_request" in exc.value.message
        bad2 = make_spec(fake_fixture, mode="live", model_request={"provider": "fake", "model": "m", "prompt_id": "p", "schema_id": "s"})
        with pytest.raises(ExperimentError) as exc2:
            ExperimentSpec.from_dict(bad2)
        assert "budget" in exc2.value.message

    def test_variant_closed(self, fake_fixture: dict[str, str]) -> None:
        with pytest.raises(ExperimentError):
            ExperimentSpec.from_dict(make_spec(fake_fixture, variant="experimental"))

    def test_hash_changes_when_prompt_changes(self, fake_fixture: dict[str, str]) -> None:
        a = ExperimentSpec.from_dict(make_spec(fake_fixture, variant="baseline"))
        b = ExperimentSpec.from_dict(make_spec(fake_fixture, variant="candidate"))
        assert a.spec_hash != b.spec_hash

    def test_hash_is_canonical_across_key_order(self, fake_fixture: dict[str, str]) -> None:
        import json

        spec_dict = make_spec(fake_fixture)
        reordered = dict(reversed(list(spec_dict.items())))
        assert json_sha256(spec_dict) == json_sha256(json.loads(json.dumps(reordered)))


class TestProviderCallRequest:
    def test_request_identity_excludes_call_id(self) -> None:
        payload = {"task": "t", "variant": "baseline"}
        a = ProviderCallRequest(call_id="call-1", provider="fake", model="m", payload=payload)
        b = ProviderCallRequest(call_id="call-2", provider="fake", model="m", payload=payload)
        assert a.request_hash == b.request_hash

    def test_payload_change_changes_hash(self) -> None:
        a = ProviderCallRequest(call_id="c", provider="fake", model="m", payload={"v": "baseline"})
        b = ProviderCallRequest(call_id="c", provider="fake", model="m", payload={"v": "candidate"})
        assert a.request_hash != b.request_hash

    def test_unknown_keys_rejected(self) -> None:
        with pytest.raises(ExperimentError):
            ProviderCallRequest.from_dict(
                {
                    "schema": "ProviderCallRequest/v1",
                    "call_id": "c",
                    "provider": "fake",
                    "model": "m",
                    "payload": {},
                    "authorization": "Bearer leaked",
                }
            )
