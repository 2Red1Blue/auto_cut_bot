"""Keep repository-local authority tools ahead of unrelated site packages."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
AUTHORITY_TOOL_ROOT = REPOSITORY_ROOT / "tools"
if str(AUTHORITY_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTHORITY_TOOL_ROOT))
