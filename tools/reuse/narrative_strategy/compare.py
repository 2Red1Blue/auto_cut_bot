"""A/B comparison report for two narrative shadow experiment attempts.

Usage:
    uv run python -m tools.reuse.narrative_strategy.compare \
        --baseline <attempt-dir> --candidate <attempt-dir> [--out report.md]

Reads only shadow artifacts (metadata/projection/metrics) — it never writes a
Store and never claims production admission. The report carries the frozen
scope note: structural/protocol metrics only; narrative quality needs blind
evaluation on video-verified labels.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPE_NOTE = (
    "> Scope: shadow A/B on structural/protocol metrics. Narrative quality claims require "
    "blind evaluation and video-verified labels per docs/open-source-adoption-phases/01-r1-narrative-strategy.md; "
    "this report alone never registers a production strategy (R1B)."
)


def _load(attempt: Path) -> dict[str, object]:
    meta = json.loads((attempt / "metadata.json").read_text(encoding="utf-8"))
    metrics = json.loads((attempt / "metrics.json").read_text(encoding="utf-8"))
    projection = json.loads(
        (attempt / "projection" / "projection.json").read_text(encoding="utf-8")
    )
    return {"metadata": meta, "metrics": metrics, "projection": projection}


def build_report(baseline_dir: Path, candidate_dir: Path) -> str:
    base = _load(baseline_dir)
    cand = _load(candidate_dir)
    lines = [
        "# Narrative strategy shadow A/B",
        "",
        f"- baseline attempt: `{baseline_dir}` (status `{base['metadata'].get('status')}`)",
        f"- candidate attempt: `{candidate_dir}` (status `{cand['metadata'].get('status')}`)",
        "",
        "| metric | baseline | candidate |",
        "|---|---|---|",
    ]
    base_metrics = {row["metric"]: row for row in base["metrics"]["results"]}  # type: ignore[index]
    cand_metrics = {row["metric"]: row for row in cand["metrics"]["results"]}  # type: ignore[index]
    for name in sorted(set(base_metrics) | set(cand_metrics)):
        b = base_metrics.get(name)
        c = cand_metrics.get(name)

        def fmt(row: dict[str, object] | None) -> str:
            if row is None:
                return "—"
            value = row.get("value")
            if value is None:
                return f"null ({row.get('missing_reason')})"
            return f"{value} ({row.get('num')}/{row.get('denom')})"

        lines.append(f"| {name} | {fmt(b)} | {fmt(c)} |")
    base_item = (base["projection"].get("items") or [{}])[0]  # type: ignore[union-attr]
    cand_item = (cand["projection"].get("items") or [{}])[0]  # type: ignore[union-attr]
    lines += [
        "",
        f"- baseline proposals: {base_item.get('proposal_count', 'n/a')}, "
        f"decode_ok: {base_item.get('decode_ok', 'n/a')}",
        f"- candidate proposals: {cand_item.get('proposal_count', 'n/a')}, "
        f"decode_ok: {cand_item.get('decode_ok', 'n/a')}, duties: {cand_item.get('duty_count', 'n/a')}",
        "",
        SCOPE_NOTE,
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = build_report(args.baseline, args.candidate)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
