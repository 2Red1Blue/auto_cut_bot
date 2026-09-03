"""Stable public identities used by package and provider integrations."""

from types import MappingProxyType

DISTRIBUTION_NAME = "auto-cut-bot-ai"
PROJECT_REPOSITORY_URL = "https://github.com/2Red1Blue/auto_cut_bot"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION_NAME}/json"
PYPI_PROJECT_URL = f"https://pypi.org/project/{DISTRIBUTION_NAME}/"
OPENROUTER_ATTRIBUTION_HEADERS = MappingProxyType(
    {
        "HTTP-Referer": PROJECT_REPOSITORY_URL,
        "X-OpenRouter-Title": "auto_cut_bot",
        "X-OpenRouter-Categories": "cli-agent,personal-agent",
    }
)
