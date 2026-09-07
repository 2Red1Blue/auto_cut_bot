"""Config switch for the media workflow tools (R3 registration gate)."""

from __future__ import annotations

from auto_cut_bot.config_base import Base


class MediaWorkflowToolConfig(Base):
    """Registration switch for media_inspect / media_plan / media_execute.

    enabled: registers media_inspect and media_plan (read-only) — on by
    default per the R3 contract. execute_enabled: media_execute stays off
    until the runtime integration passes its exit checks."""

    enabled: bool = True
    execute_enabled: bool = False
