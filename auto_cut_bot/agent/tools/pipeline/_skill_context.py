"""Skill context injection for domain agent task prompts.

Instead of hardcoding tool names and descriptions in Python prompts,
this module loads Skill file content and injects it into the sub-agent's
system prompt. The Skill file is the single source of truth for:

- Which tools are available in this domain
- How to use them (order, parameters, fallback)
- Data layer integration (query tools, validation, context packing)
- Writing discipline rules and editorial contracts

Usage:
    from auto_cut_bot.agent.tools.pipeline._skill_context import inject_skill_context

    prompt = inject_skill_context(["ac_story_generation"])
    # Returns formatted Skill body content ready for prompt injection
"""

from __future__ import annotations

from pathlib import Path

from auto_cut_bot.agent.skills import SkillsLoader

# Module-level loader instance — workspace is the standard auto_cut_bot workspace
_loader = SkillsLoader(workspace=Path("~/.auto_cut_bot/workspace").expanduser())


def inject_skill_context(skill_names: list[str]) -> str:
    """Load Skill files and return formatted content for prompt injection.

    Args:
        skill_names: List of skill directory names (e.g. ["ac_story_generation"]).

    Returns:
        Formatted Skill content ready to inject into a sub-agent task prompt.
        Returns empty string if no skills are found.
    """
    return _loader.load_skills_for_context(skill_names)