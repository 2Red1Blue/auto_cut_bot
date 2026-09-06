#!/usr/bin/env python3
"""Fixture runner skeleton for open-source reference comparisons.

See docs/llm-stage-contracts/12-reuse-fixture-runner.md for the conventions this
skeleton enforces. Copy this file into tools/reuse/<producer>/run.py and fill in
the PRODUCER block; do not weaken the guardrails.

Guarantees:
- refuses to run when the producer repo HEAD != ledger-pinned commit
- blocks outbound network for the child process (best effort: env + sigset hint)
- writes everything under artifacts/shadow/<producer>/<run_id>/ only
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# --- PRODUCER block: fill in per producer -----------------------------------
PRODUCER = "PRODUCER_NAME"
PRODUCER_REPO = REPO_ROOT.parent / "reference-projects" / "PROJECT_DIR"
PINNED_COMMIT = ""  # must match governance/reuse.yaml + doc 11 §2
# ----------------------------------------------------------------------------

SHADOW_ROOT = REPO_ROOT / "artifacts" / "shadow"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_offline_env(env: dict[str, str]) -> dict[str, str]:
    """Neutralize outbound network for the child process (best effort)."""
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    ):
        env[key] = ""
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["NO_PROXY"] = "*"
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture-manifest", type=Path, required=True,
                    help="docs/llm-stage-contracts/fixtures/<set>/manifest.yaml")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="override shadow output dir (must stay under artifacts/shadow/)")
    ap.add_argument("--producer-repo", type=Path, default=PRODUCER_REPO)
    args = ap.parse_args(argv)

    head = subprocess.run(
        ["git", "-C", str(args.producer_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode != 0 or head.stdout.strip() != PINNED_COMMIT:
        print(
            f"REFUSING: producer repo HEAD {head.stdout.strip() or '<unavailable>'} "
            f"!= pinned {PINNED_COMMIT}; see governance/reuse.yaml",
            file=sys.stderr,
        )
        return 2

    manifest = args.fixture_manifest.resolve()
    if not manifest.is_file():
        print(f"REFUSING: fixture manifest not found: {manifest}", file=sys.stderr)
        return 2
    if REPO_ROOT not in manifest.parents:
        print("REFUSING: fixture manifest must live inside the repo", file=sys.stderr)
        return 2

    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (SHADOW_ROOT / PRODUCER / run_id)
    out_dir = out_dir.resolve()
    if SHADOW_ROOT.resolve() not in out_dir.parents:
        print(f"REFUSING: output must stay under {SHADOW_ROOT}", file=sys.stderr)
        return 2
    for sub in ("raw", "projection", "metrics.json", "metadata.json"):
        if (out_dir / sub).exists():
            print(f"REFUSING: output already exists: {out_dir / sub}", file=sys.stderr)
            return 2
    (out_dir / "raw").mkdir(parents=True, exist_ok=False)

    metadata = {
        "producer": PRODUCER,
        "producer_commit": head.stdout.strip(),
        "fixture_manifest": str(manifest.relative_to(REPO_ROOT)),
        "fixture_manifest_sha256": _sha256(manifest),
        "runner_commit": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip(),
        "started_utc": run_id,
        "offline_env": True,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # --- producer invocation goes here ---------------------------------------
    # Run the external project against the fixture inputs, writing raw output
    # into out_dir/"raw". Keep the invocation offline via _assert_offline_env.
    # Capture its own result files unmodified (do not edit producer output).
    raise NotImplementedError("fill in the producer invocation for this producer")

    # --- projection + metrics ------------------------------------------------
    # projection/: map raw output onto project DTOs, deterministically, and note
    # any un-mappable fields. metrics.json: see doc 12 §4 for required keys.
    # Then write metrics.json and update metadata.json with finished_utc.


if __name__ == "__main__":
    sys.exit(main())
