"""Ordered local measurement corpus, results and unaccepted replay report.

An ordinal belongs to one exact case/request pair, never merely to a source.
The manifest defines the caller's complete ordered corpus; this pure module
cannot authenticate that choice, its source provenance or any native execution.
Raw responses are external bytes keyed by (ordinal, case hash), not blob IDs.
No Record, acceptance bound, persistence receipt or installation is produced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from .local_speech_window import LocalSpeechWindowRequest
from .local_speech_window_codec import decode_local_speech_window_request
from .shadow_local_calibration import ShadowLocalCalibrationCase, build_shadow_local_request
from .shadow_local_measurement import ShadowLocalMeasurementEvidence
from .types import canonical_sha256

RawResponseKey = tuple[int, str]
RawResponses = Mapping[RawResponseKey, bytes]
_MANIFEST_SCHEMA = "shadow-local-measurement-manifest-v1"
_RESULTS_SCHEMA = "shadow-local-measurement-results-v1"
_REPORT_SCHEMA = "shadow-local-measurement-validation-report-v1"


class ShadowLocalMeasurementSetError(ValueError):
    """The exact ordered corpus or its raw-recomputed content does not close."""


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ShadowLocalMeasurementSetError("measurement set value must be an exact object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != fields:
        raise ShadowLocalMeasurementSetError("measurement set object has missing or unknown fields")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:
        raise ShadowLocalMeasurementSetError("measurement set wire members must be an array")
    return cast(list[object], value)


def _ordinal(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ShadowLocalMeasurementSetError("member ordinal must be an exact nonnegative integer")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ShadowLocalMeasurementSetError("invalid canonical measurement set content") from error


def _equal(actual: object, expected: object) -> None:
    if _canonical(actual) != _canonical(expected):
        raise ShadowLocalMeasurementSetError("measurement set differs from exact ordered recomputation")


def _evidence_tuple(value: object) -> tuple[ShadowLocalMeasurementEvidence, ...]:
    if type(value) is not tuple:
        raise ShadowLocalMeasurementSetError("evidence must be an exact nonempty tuple")
    items = cast(tuple[object, ...], value)
    if not items or any(type(item) is not ShadowLocalMeasurementEvidence for item in items):
        raise ShadowLocalMeasurementSetError("evidence must contain exact local measurements")
    return cast(tuple[ShadowLocalMeasurementEvidence, ...], items)


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementManifestMember:
    ordinal: int
    case: ShadowLocalCalibrationCase
    request: LocalSpeechWindowRequest

    def __post_init__(self) -> None:
        _ordinal(self.ordinal)
        if type(self.case) is not ShadowLocalCalibrationCase or type(self.request) is not LocalSpeechWindowRequest:
            raise ShadowLocalMeasurementSetError("manifest member requires exact case/request values")
        expected = build_shadow_local_request(self.case, max_response_bytes=self.request.max_response_bytes)
        _equal(self.request.to_mapping(), expected.to_mapping())

    @property
    def raw_response_key(self) -> RawResponseKey:
        return self.ordinal, self.case.canonical_hash

    def to_mapping(self) -> dict[str, object]:
        return {"ordinal": self.ordinal, "case": self.case.to_mapping(), "request": self.request.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> ShadowLocalMeasurementManifestMember:
        raw = _object(value, {"ordinal", "case", "request"})
        try:
            return cls(_ordinal(raw["ordinal"]), ShadowLocalCalibrationCase.from_mapping(raw["case"]),
                       decode_local_speech_window_request(raw["request"]))
        except ValueError as error:
            raise ShadowLocalMeasurementSetError("invalid manifest case/request pair") from error


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementManifest:
    """Complete input corpus, including each case's independent anchors."""

    members: tuple[ShadowLocalMeasurementManifestMember, ...]

    def __post_init__(self) -> None:
        if (type(self.members) is not tuple or not self.members
                or any(type(member) is not ShadowLocalMeasurementManifestMember for member in self.members)):
            raise ShadowLocalMeasurementSetError("manifest requires exact nonempty member tuple")
        if tuple(member.ordinal for member in self.members) != tuple(range(len(self.members))):
            raise ShadowLocalMeasurementSetError("manifest ordinals must be contiguous in original order")
        cases = tuple(member.case.canonical_hash for member in self.members)
        if len(cases) != len(set(cases)):
            raise ShadowLocalMeasurementSetError("duplicate exact case in measurement corpus")

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": _MANIFEST_SCHEMA, "members": [member.to_mapping() for member in self.members]}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object) -> ShadowLocalMeasurementManifest:
        raw = _object(value, {"schema_version", "members"})
        manifest = cls(tuple(ShadowLocalMeasurementManifestMember.from_mapping(member)
                             for member in _array(raw["members"])))
        _equal(raw, manifest.to_mapping())
        return manifest

    @classmethod
    def from_evidence(cls, evidence: tuple[ShadowLocalMeasurementEvidence, ...]) -> ShadowLocalMeasurementManifest:
        return cls(tuple(ShadowLocalMeasurementManifestMember(ordinal, item.case, item.request)
                         for ordinal, item in enumerate(_evidence_tuple(evidence))))


def _responses(manifest: ShadowLocalMeasurementManifest, responses: object) -> dict[RawResponseKey, bytes]:
    """Check all lookup keys/byte ceilings before replaying the first member."""
    if not isinstance(responses, Mapping):
        raise ShadowLocalMeasurementSetError("raw responses require an ordinal/case-keyed mapping")
    expected = {member.raw_response_key: member for member in manifest.members}
    checked: dict[RawResponseKey, bytes] = {}
    for key, raw in cast(Mapping[object, object], responses).items():
        if type(key) is not tuple:
            raise ShadowLocalMeasurementSetError("raw response key must be exact (ordinal, case hash)")
        parts = cast(tuple[object, ...], key)
        if len(parts) != 2 or type(parts[1]) is not str:
            raise ShadowLocalMeasurementSetError("raw response key must contain exactly ordinal/case identity")
        pair = (_ordinal(parts[0]), parts[1])
        member = expected.get(pair)
        if member is None or pair in checked:
            raise ShadowLocalMeasurementSetError("unexpected or duplicate raw response identity")
        if type(raw) is not bytes or not raw or len(raw) > member.request.max_response_bytes:
            raise ShadowLocalMeasurementSetError("raw response violates its exact member byte bound")
        checked[pair] = raw
    if set(checked) != set(expected):
        raise ShadowLocalMeasurementSetError("raw responses omit required measurement members")
    return checked


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementResults:
    manifest: ShadowLocalMeasurementManifest
    evidence: tuple[ShadowLocalMeasurementEvidence, ...]

    def __post_init__(self) -> None:
        if type(self.manifest) is not ShadowLocalMeasurementManifest:
            raise ShadowLocalMeasurementSetError("results require an exact manifest")
        evidence = _evidence_tuple(self.evidence)
        if len(evidence) != len(self.manifest.members):
            raise ShadowLocalMeasurementSetError("results must preserve the entire manifest corpus")
        for member, item in zip(self.manifest.members, evidence, strict=True):
            _equal(member.case.to_mapping(), item.case.to_mapping())
            _equal(member.request.to_mapping(), item.request.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _RESULTS_SCHEMA,
            "manifest_sha256": self.manifest.canonical_hash,
            "members": [{"ordinal": member.ordinal, "case_sha256": member.case.canonical_hash,
                         "request_sha256": member.request.canonical_hash, "evidence": item.to_mapping()}
                        for member, item in zip(self.manifest.members, self.evidence, strict=True)],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(
        cls, value: object, *, manifest: ShadowLocalMeasurementManifest, raw_responses: RawResponses,
    ) -> ShadowLocalMeasurementResults:
        if type(manifest) is not ShadowLocalMeasurementManifest:
            raise ShadowLocalMeasurementSetError("results decoder requires an exact manifest")
        raw = _object(value, {"schema_version", "manifest_sha256", "members"})
        _equal(raw["schema_version"], _RESULTS_SCHEMA)
        _equal(raw["manifest_sha256"], manifest.canonical_hash)
        members = _array(raw["members"])
        if len(members) != len(manifest.members):
            raise ShadowLocalMeasurementSetError("result member count differs from manifest")
        responses = _responses(manifest, raw_responses)
        payloads: list[object] = []
        for member, claimed in zip(manifest.members, members, strict=True):
            row = _object(claimed, {"ordinal", "case_sha256", "request_sha256", "evidence"})
            _equal([row["ordinal"], row["case_sha256"], row["request_sha256"]],
                   [member.ordinal, member.case.canonical_hash, member.request.canonical_hash])
            payloads.append(row["evidence"])
        try:
            evidence = tuple(ShadowLocalMeasurementEvidence.from_mapping(
                payload, raw_response=responses[member.raw_response_key],
            ) for member, payload in zip(manifest.members, payloads, strict=True))
            results = cls(manifest, evidence)
        except ValueError as error:
            raise ShadowLocalMeasurementSetError("result evidence failed independent raw replay") from error
        _equal(raw, results.to_mapping())
        return results


def _case_report(
    member: ShadowLocalMeasurementManifestMember, evidence: ShadowLocalMeasurementEvidence,
) -> dict[str, object]:
    projection = cast(dict[str, object], evidence.to_mapping()["projection"])
    spec = member.case.extraction
    return {
        "ordinal": member.ordinal, "case_sha256": member.case.canonical_hash,
        "request_sha256": member.request.canonical_hash,
        "source_id": spec.source_id, "source_sha256": spec.source_sha256,
        "clock_id": spec.clock_id,
        "time_base": {"numerator": spec.time_base.numerator, "denominator": spec.time_base.denominator},
        "asr": {"matches": projection["asr_matches"], "maximum_absolute_tick": max(
            (match.absolute_tick for match in evidence.projection.asr_matches), default=None)},
        "vad": {"matches": projection["vad_matches"], "maximum_absolute_tick": max(
            (match.absolute_tick for match in evidence.projection.vad_matches), default=None)},
    }


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementValidationReport:
    """Independent raw replay observations, deliberately without a verdict.

    Tick maxima are per case/role only. Empty observations have no measured
    maximum (null), while actual zero-error matches retain numeric zero.
    """

    results: ShadowLocalMeasurementResults

    def __post_init__(self) -> None:
        if type(self.results) is not ShadowLocalMeasurementResults:
            raise ShadowLocalMeasurementSetError("report requires exact measurement results")
        results = self.results
        replayed = ShadowLocalMeasurementResults.from_mapping(
            results.to_mapping(), manifest=results.manifest,
            raw_responses={member.raw_response_key: evidence.raw_response
                           for member, evidence in zip(results.manifest.members, results.evidence, strict=True)},
        )
        object.__setattr__(self, "results", replayed)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _REPORT_SCHEMA,
            "manifest_sha256": self.results.manifest.canonical_hash,
            "results_sha256": self.results.canonical_hash,
            "members": [_case_report(member, evidence) for member, evidence in zip(
                self.results.manifest.members, self.results.evidence, strict=True)],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(
        cls, value: object, *, results: ShadowLocalMeasurementResults, raw_responses: RawResponses,
    ) -> ShadowLocalMeasurementValidationReport:
        if type(results) is not ShadowLocalMeasurementResults:
            raise ShadowLocalMeasurementSetError("report decoder requires exact measurement results")
        raw = _object(value, {"schema_version", "manifest_sha256", "results_sha256", "members"})
        responses = _responses(results.manifest, raw_responses)
        for member, evidence in zip(results.manifest.members, results.evidence, strict=True):
            if responses[member.raw_response_key] != evidence.raw_response:
                raise ShadowLocalMeasurementSetError("report supplied raw bytes differ from exact result bytes")
        report = cls(results)  # Replays the now byte-equal responses, not a claimed verdict.
        _equal(raw, report.to_mapping())
        return report
