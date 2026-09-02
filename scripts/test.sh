#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPOSITORY_ROOT}:${REPOSITORY_ROOT}/packages/autocut-core:${REPOSITORY_ROOT}/packages/autocut-kernel/src${PYTHONPATH:+:${PYTHONPATH}}"

exec uv run pytest "$@"
