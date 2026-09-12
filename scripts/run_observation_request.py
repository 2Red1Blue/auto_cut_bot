"""Read, status, or explicitly execute one standalone rich-observation canary."""
from __future__ import annotations

import argparse
import json

from auto_cut_bot.pipeline.runtime.observation_composition import (
    compose_observation_entry_from_environment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--episode-index", required=True, type=int)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    entry = compose_observation_entry_from_environment()
    if entry is None:
        print(json.dumps({"status":"error","error_code":"OBSERVATION_RUNTIME_NOT_CONFIGURED"}))
        return 2
    try:
        if args.status:
            outcome = entry.status(args.source_run_id, args.episode_index)
            state = "not_started" if outcome is None else outcome.state
            code = 0 if state == "succeeded" else 1 if state in ("denied", "failed") else 3
            print(json.dumps({"status": state, "scope": "observation_only", "provider_calls": 0}, separators=(",", ":")))
            return code
        if not args.execute:
            request = entry.dry_run(args.source_run_id, args.episode_index)
            print(json.dumps({"status": "dry_run", "scope": "observation_only", "provider_calls": 0,
                              "request_hash": request.request_hash}, separators=(",", ":")))
            return 0
        result = entry.execute(args.source_run_id, args.episode_index)
        state = result.outcome.state
        print(json.dumps({"status": state, "scope": "observation_only"}, separators=(",", ":")))
        return 0 if state == "succeeded" else 1 if state in ("denied", "failed") else 3
    except Exception as error:
        print(json.dumps({"status":"error","error_code":"OBSERVATION_REQUEST_REJECTED","exception_kind":type(error).__name__}))
        return 2
if __name__ == "__main__":
    raise SystemExit(main())
