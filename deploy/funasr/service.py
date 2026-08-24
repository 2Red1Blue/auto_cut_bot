"""Standalone SenseVoiceSmall + FSMN-VAD timed evidence service."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib.metadata
import json
import os
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import torch
from aiohttp import web
from funasr import AutoModel


def canon(v: object) -> bytes:
    return json.dumps(v, sort_keys=True, separators=(",", ":")).encode()


def sha(v: bytes) -> str:
    return "sha256:" + hashlib.sha256(v).hexdigest()


def tick(ms: int, tb: dict[str, int], end: bool) -> int:
    v = Fraction(ms, 1000) * tb["denominator"] / tb["numerator"]
    return -((-v.numerator) // v.denominator) if end else v.numerator // v.denominator


def tree_hash(path: Path) -> str:
    records = []
    for p in sorted(path.rglob("*")):
        if p.is_symlink():
            raise RuntimeError("model symlink forbidden")
        if p.is_file():
            digest = hashlib.sha256()
            size = 0
            with p.open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
            records.append(
                {
                    "path": p.relative_to(path).as_posix(),
                    "size": size,
                    "sha256": "sha256:" + digest.hexdigest(),
                }
            )
    return sha(canon(records))


def coverage(m: dict[str, object], outcome: str) -> dict[str, object]:
    s = cast(dict[str, object], m["source"])
    c = cast(dict[str, object], m["audio_clock"])
    r = cast(dict[str, object], m["requested_range"])
    return {
        "source_id": s["source_id"],
        "source_sha256": s["source_sha256"],
        "clock_id": c["clock_id"],
        "time_base": c["time_base"],
        "in_tick": r["in_tick"],
        "out_tick": r["out_tick"],
        "outcome": outcome,
    }


def empty_transcript(required: bool, indeterminate: bool = False) -> dict[str, object]:
    return {
        "coverage_outcome": "partial" if indeterminate else "complete",
        "outcome": "indeterminate" if indeterminate else "no_lexical_content",
        "completeness": {
            "segment": "partial" if indeterminate else "complete",
            "word": "partial"
            if indeterminate and required
            else "complete"
            if required
            else "not_applicable",
            "sentence": "partial" if indeterminate else "complete",
        },
        "segments": [],
        "words": [],
        "sentences": [],
        "boundary_touch": {"left": False, "right": False},
        "truncated": False,
    }


def transcript(
    raw: object, tb: dict[str, int], rr: dict[str, int], required: bool, gap: int
) -> dict[str, object]:
    bad = empty_transcript(required, True)
    if type(raw) is not list or len(raw) != 1 or type(raw[0]) is not dict:
        return bad
    x = raw[0]
    text = x.get("text")
    ts = x.get("timestamp")
    words = x.get("words")
    if required:
        if (
            type(text) is str
            and not text.strip()
            and type(ts) is list
            and not ts
            and (words is None or words == [])
        ):
            return empty_transcript(True)
        if (
            type(text) is not str
            or type(words) is not list
            or type(ts) is not list
            or not words
            or len(words) != len(ts)
        ):
            return bad
        out = []
        ms = []
        try:
            for n, (w, pair) in enumerate(zip(words, ts, strict=True)):
                a, b = pair
                if (
                    type(w) is not str
                    or not w.strip()
                    or type(a) is not int
                    or type(b) is not int
                    or a < 0
                    or a >= b
                    or (ms and ms[-1][1] > a)
                ):
                    raise ValueError
                i = rr["in_tick"] + tick(a, tb, False)
                o = rr["in_tick"] + tick(b, tb, True)
                if i < rr["in_tick"] or o > rr["out_tick"]:
                    raise ValueError
                out.append(
                    {"word_id": f"word-{n:08d}", "in_tick": i, "out_tick": o, "text": w.strip()}
                )
                ms.append((a, b))
        except Exception:
            return bad
        ranges = []
        start = 0
        for n in range(1, len(out)):
            if ms[n][0] - ms[n - 1][1] > gap:
                ranges.append((start, n))
                start = n
        ranges.append((start, len(out)))
        sent = []
        seg = []
        for n, (a, b) in enumerate(ranges):
            selected = out[a:b]
            sid = f"sentence-{n:08d}"
            txt = "".join(z["text"] for z in selected)
            sent.append(
                {
                    "sentence_id": sid,
                    "in_tick": selected[0]["in_tick"],
                    "out_tick": selected[-1]["out_tick"],
                    "word_ids": [z["word_id"] for z in selected],
                    "text": txt,
                }
            )
            seg.append(
                {
                    "segment_id": f"segment-{n:08d}",
                    "in_tick": selected[0]["in_tick"],
                    "out_tick": selected[-1]["out_tick"],
                    "sentence_ids": [sid],
                    "text": txt,
                }
            )
        return {
            "coverage_outcome": "complete",
            "outcome": "transcript_available",
            "completeness": {"segment": "complete", "word": "complete", "sentence": "complete"},
            "segments": seg,
            "words": out,
            "sentences": sent,
            "boundary_touch": {
                "left": out[0]["in_tick"] <= rr["in_tick"],
                "right": out[-1]["out_tick"] >= rr["out_tick"],
            },
            "truncated": False,
        }
    return bad


def vad(
    raw: object, tb: dict[str, int], rr: dict[str, int], gap: int
) -> tuple[str, list[dict[str, object]]]:
    if (
        type(raw) is not list
        or len(raw) != 1
        or type(raw[0]) is not dict
        or type(raw[0].get("value")) is not list
    ):
        return "indeterminate", []
    values = raw[0]["value"]
    if not values:
        return "no_speech", []
    merged = []
    try:
        for a, b in values:
            if (
                type(a) is not int
                or type(b) is not int
                or a < 0
                or a >= b
                or (merged and merged[-1][0] > a)
            ):
                raise ValueError
            if merged and a - merged[-1][1] <= gap:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        result = []
        for n, (a, b) in enumerate(merged):
            i = rr["in_tick"] + tick(a, tb, False)
            o = rr["in_tick"] + tick(b, tb, True)
            if i < rr["in_tick"] or o > rr["out_tick"]:
                raise ValueError
            result.append(
                {
                    "speech_segment_id": f"speech-{n:08d}",
                    "in_tick": i,
                    "out_tick": o,
                    "confidence_ppm": None,
                }
            )
        return "speech", result
    except Exception:
        return "indeterminate", []


class Service:
    def __init__(self) -> None:
        self.profile = json.loads(os.environ["FUNASR_PROFILE_JSON"])
        self.lock = asyncio.Lock()
        self.ready = False
        self.max_request = int(os.environ["FUNASR_MAX_REQUEST_BYTES"])
        self.max_response = int(os.environ["FUNASR_MAX_RESPONSE_BYTES"])
        self.timeout = int(os.environ["FUNASR_INFERENCE_TIMEOUT_SECONDS"])
        self.model: Any = None
        self.measured = None

    async def load(self) -> None:
        a = Path(os.environ["FUNASR_ASR_MODEL_PATH"]).resolve()
        v = Path(os.environ["FUNASR_VAD_MODEL_PATH"]).resolve()
        measured = dict(self.profile)
        measured.update(
            funasr_version=importlib.metadata.version("funasr"),
            torch_version=torch.__version__,
            asr_model_sha256=await asyncio.to_thread(tree_hash, a),
            vad_model_sha256=await asyncio.to_thread(tree_hash, v),
        )
        for k in ("funasr_version", "torch_version", "asr_model_sha256", "vad_model_sha256"):
            if measured[k] != self.profile[k]:
                raise RuntimeError(f"identity mismatch {k}")
        self.measured = sha(canon(measured))
        self.model = await asyncio.to_thread(
            AutoModel,
            model=str(a),
            vad_model=str(v),
            device=self.profile["device"],
            disable_update=True,
            disable_pbar=True,
        )
        self.ready = True

    def infer(self, p: Path) -> tuple[object, object]:
        return self.model.generate(input=str(p), output_timestamp=True), self.model.inference(
            str(p), model=self.model.vad_model, kwargs=copy.deepcopy(self.model.vad_kwargs)
        )

    async def evidence(self, req: web.Request) -> web.Response:
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        try:
            m = json.loads(base64.b64decode(req.headers["X-Timed-Speech-Manifest"], validate=True))
        except Exception as e:
            raise web.HTTPBadRequest(text="bad manifest") from e
        if sha(canon(m)) != req.headers.get("X-Timed-Speech-Request-SHA256"):
            raise web.HTTPBadRequest(text="identity")
        rr = m["requested_range"]
        c = m["audio_clock"]
        if rr != {"in_tick": c["origin_tick"], "out_tick": c["origin_tick"] + c["duration_tick"]}:
            raise web.HTTPBadRequest(text="full source only")
        with tempfile.TemporaryDirectory(prefix="funasr-request-") as d:
            p = Path(d) / "source.mp4"
            h = hashlib.sha256()
            size = 0
            with p.open("xb") as f:
                async for chunk in req.content.iter_chunked(1 << 20):
                    size += len(chunk)
                    if size > self.max_request:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=self.max_request, actual_size=size
                        )
                    h.update(chunk)
                    f.write(chunk)
            if "sha256:" + h.hexdigest() != m["source"]["source_sha256"]:
                raise web.HTTPBadRequest(text="source hash")
            async with self.lock:
                try:
                    a, v = await asyncio.wait_for(
                        asyncio.shield(asyncio.to_thread(self.infer, p)), self.timeout
                    )
                except TimeoutError:
                    os._exit(70)
            state, vsegs = vad(
                v, c["time_base"], rr, m["timing_policy"]["vad_merge_gap_milliseconds"]
            )
            t = transcript(
                a,
                c["time_base"],
                rr,
                m["profile"]["word_timing_capability"] == "required",
                m["timing_policy"]["utterance_gap_milliseconds"],
            )
            if t["outcome"] == "no_lexical_content" and state == "no_speech":
                t["outcome"] = "no_speech"
            t["coverage"] = coverage(m, t.pop("coverage_outcome"))
            speech = {
                "coverage": coverage(m, "partial" if state == "indeterminate" else "complete"),
                "outcome": {
                    "speech": "speech_detected",
                    "no_speech": "none_detected",
                    "indeterminate": "indeterminate",
                }[state],
                "segments": vsegs,
            }
            identities = []
            for e in m["expected_producers"]:
                identities.append(
                    {
                        **{k: e[k] for k in e if k != "timing_error_bound_tick"},
                        **{
                            k: self.profile[k]
                            for k in (
                                "provider_id",
                                "provider_version",
                                "funasr_version",
                                "torch_version",
                                "device",
                            )
                        },
                    }
                )
            bounds = {
                e["producer_kind"]: {
                    "early_tick": e["timing_error_bound_tick"],
                    "late_tick": e["timing_error_bound_tick"],
                    "time_base": c["time_base"],
                }
                for e in m["expected_producers"]
            }
            response = {
                "schema_version": "timed-speech-evidence-response-v1",
                "request_identity_sha256": sha(canon(m)),
                "source": m["source"],
                "container": m["container"],
                "audio_clock": c,
                "requested_range": rr,
                "timed_speech_policy_sha256": m["timed_speech_policy_sha256"],
                "transcript_capability": m["transcript_capability"],
                "producer_identities": identities,
                "timing_error_bounds": bounds,
                "transcript": t,
                "speech_activity": speech,
            }
            raw = canon(response)
            if len(raw) > min(self.max_response, m["response_limits"]["max_response_bytes"]):
                raise web.HTTPInternalServerError(text="response bound")
            return web.Response(body=raw, content_type="application/json")


async def startup(app: web.Application) -> None:
    await app["service"].load()


def main() -> None:
    s = Service()
    app = web.Application(client_max_size=s.max_request)
    app["service"] = s
    app.add_routes(
        [
            web.get("/health/live", lambda _: web.json_response({"status": "live"})),
            web.get(
                "/health/ready",
                lambda _: web.json_response(
                    {
                        "status": "ready" if s.ready else "loading",
                        "profile_identity_sha256": s.measured,
                    },
                    status=200 if s.ready else 503,
                ),
            ),
            web.post("/v1/timed-speech-evidence", s.evidence),
        ]
    )
    app.on_startup.append(startup)
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("FUNASR_PORT", "8765")))


if __name__ == "__main__":
    main()
