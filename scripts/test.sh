#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPOSITORY_ROOT}:${REPOSITORY_ROOT}/tools:${REPOSITORY_ROOT}/packages/autocut-core:${REPOSITORY_ROOT}/packages/autocut-kernel/src${PYTHONPATH:+:${PYTHONPATH}}"

# pytest collects from the working directory; run from the repository root so
# callers outside the tree do not sweep unrelated projects into collection.
cd -- "$REPOSITORY_ROOT"

exec uv run pytest "$@"
