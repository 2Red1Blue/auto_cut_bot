"""F0 producer-time guard: handoff files are deliberately absent here."""

from __future__ import annotations

import argparse
from pathlib import Path

from .model import IntakeError


def verify_handoff(source_root: Path) -> None:
    for name in ("f0-review.json", "f0-handoff.json"):
        if (source_root / name).exists():
            raise IntakeError("F0 handoff metadata belongs only to the attestation child")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    try:
        verify_handoff(args.source_root)
    except IntakeError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
