"""Validate F0 ledger evidence."""
# pyright: reportUnknownVariableType=none, reportUnknownArgumentType=none, reportUnknownMemberType=none, reportUnnecessaryComparison=none

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import PIN_IDS, SLOT_IDS, IntakeError, reject_forbidden
from .verify_inputs import load_jcs


def verify_ledger(
    ledger_path: Path, spans_path: Path, ac_path: Path, manifest_path: Path | None = None
) -> None:
    ledger, spans, requests = (load_jcs(path) for path in (ledger_path, spans_path, ac_path))
    if (
        set(ledger) != {"format", "contract_version", "slots"}
        or ledger["format"] != "autocut.f-unresolved-authority-ledger/v1"
        or ledger["contract_version"] != "2.1.3"
    ):
        raise IntakeError("invalid ledger header")
    slots = ledger["slots"]
    if (
        not isinstance(slots, list)
        or {slot.get("slot_id") for slot in slots if isinstance(slot, dict)} != SLOT_IDS
        or len(slots) != len(SLOT_IDS)
    ):
        raise IntakeError("slot enumeration is not exact")
    source_spans: dict[str, set[tuple[object, ...]]] = {}
    if manifest_path:
        for pin in load_jcs(manifest_path)["inputs"]:
            if pin["kind"] == "availability_record":
                source_spans[pin["pin_id"]] = set()
                continue
            source_spans[pin["pin_id"]] = {
                tuple(
                    span[key] for key in ("path", "raw_sha256", "start_line", "end_line", "anchor")
                )
                for blob in pin["source_blobs"]
                for span in blob["source_spans"]
            }
    slots_by_id: dict[str, dict[str, Any]] = {}
    for slot in slots:
        required = {
            "slot_id",
            "closure_area",
            "required_fact",
            "classification",
            "authority_anchors",
            "input_pin_ids",
            "owner",
            "consumer_packages",
            "ledger_state",
            "authority_change_request_id",
            "blocking_reason",
        }
        if (
            set(slot) != required
            or slot["classification"]
            not in {
                "authority_change_required",
                "source_binding_deferred",
                "accepted_authority_input",
            }
            or slot["ledger_state"] not in {"blocked", "recorded"}
        ):
            raise IntakeError("slot is not closed")
        if (
            not isinstance(slot["input_pin_ids"], list)
            or sorted(set(slot["input_pin_ids"])) != slot["input_pin_ids"]
            or not set(slot["input_pin_ids"]) <= PIN_IDS
        ):
            raise IntakeError("slot pins invalid")
        if (slot["classification"] == "authority_change_required") != bool(
            slot["authority_change_request_id"]
        ):
            raise IntakeError("authority-change relation inconsistent")
        slots_by_id[slot["slot_id"]] = slot
    if (
        set(spans) != {"format", "contract_version", "mappings"}
        or spans["format"] != "autocut.f-source-span-map/v1"
        or spans["contract_version"] != "2.1.3"
        or not isinstance(spans["mappings"], list)
    ):
        raise IntakeError("invalid span map")
    mappings = spans["mappings"]
    if {item.get("slot_id") for item in mappings if isinstance(item, dict)} != SLOT_IDS or len(
        mappings
    ) != len(SLOT_IDS):
        raise IntakeError("source-span map must cover each slot exactly once")
    for item in mappings:
        if (
            set(item) != {"slot_id", "pin_id", "source_span", "use"}
            or item["pin_id"] not in PIN_IDS
            or item["use"] not in {"normative", "context_only"}
        ):
            raise IntakeError("invalid mapping")
        span = item["source_span"]
        if not isinstance(span, dict) or set(span) != {
            "path",
            "raw_sha256",
            "start_line",
            "end_line",
            "anchor",
        }:
            raise IntakeError("span not closed")
        if (
            manifest_path
            and tuple(
                span[key] for key in ("path", "raw_sha256", "start_line", "end_line", "anchor")
            )
            not in source_spans[item["pin_id"]]
        ):
            raise IntakeError("span not bound by manifest")
        if item["pin_id"] not in slots_by_id[item["slot_id"]]["input_pin_ids"]:
            raise IntakeError("span mapping pin is not named by its ledger slot")
    if (
        set(requests) != {"format", "contract_version", "requests"}
        or requests["format"] != "autocut.f-authority-change-request-index/v1"
        or requests["contract_version"] != "2.1.3"
        or not isinstance(requests["requests"], list)
    ):
        raise IntakeError("invalid AC index")
    required_request = {
        "request_id",
        "slot_ids",
        "required_normative_fact",
        "authority_anchors",
        "superseded_anchors",
        "affected_packages",
        "request_state",
    }
    reqs: dict[str, dict[str, Any]] = {}
    for request in requests["requests"]:
        if (
            not isinstance(request, dict)
            or set(request) != required_request
            or request["request_state"] != "required"
            or not isinstance(request["request_id"], str)
            or not request["request_id"]
            or request["request_id"] in reqs
            or not isinstance(request["slot_ids"], list)
            or not request["slot_ids"]
        ):
            raise IntakeError("AC request is not an exact required record")
        reqs[request["request_id"]] = request
    for slot in slots:
        if slot["authority_change_request_id"]:
            request = reqs.get(slot["authority_change_request_id"])
            if (
                request is None
                or request.get("request_state") != "required"
                or slot["slot_id"] not in request.get("slot_ids", [])
                or sorted(slot["authority_anchors"]) != sorted(request.get("authority_anchors", []))
            ):
                raise IntakeError("ledger AC relation not closed")
    for request_id, request in reqs.items():
        ledger_slot_ids = {
            slot["slot_id"] for slot in slots if slot["authority_change_request_id"] == request_id
        }
        if set(request["slot_ids"]) != ledger_slot_ids:
            raise IntakeError("AC request slot census is not the reverse ledger closure")
    reject_forbidden(ledger)
    reject_forbidden(spans)
    reject_forbidden(requests)
