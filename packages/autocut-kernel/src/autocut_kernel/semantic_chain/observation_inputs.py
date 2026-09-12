"""Pure, unverified observation inputs for new narrative consumers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType

from ..store.observation import PersistedObservationReport


class ObservationInputError(ValueError):
    """Committed observations cannot safely form a consumer input."""


def _alias(index: int) -> str:
    if not 0 <= index < 702:
        raise ObservationInputError("observation alias capacity is exhausted")
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    if index < 26:
        return alphabet[index]
    return alphabet[(index - 26) // 26] + alphabet[(index - 26) % 26]


@dataclass(frozen=True, slots=True)
class ObservationConsumerInputs:
    reports: tuple[PersistedObservationReport, ...]
    aliases: dict[str, int]
    descriptions: dict[str, str]

    def __post_init__(self) -> None:
        if not self.reports:
            raise ObservationInputError("consumer inputs require at least one observation report")
        if any(type(item) is not PersistedObservationReport for item in self.reports):
            raise ObservationInputError("consumer inputs require exact persisted observation reports")
        if any(item.status != "observation_unverified" for item in self.reports):
            raise ObservationInputError("only unverified observation reports are consumable")
        expected = {_alias(index): index for index in range(len(self.reports))}
        if self.aliases != expected or set(self.descriptions) != set(expected):
            raise ObservationInputError("consumer aliases must be the canonical report directory")
        object.__setattr__(self, "aliases", MappingProxyType(dict(expected)))
        object.__setattr__(self, "descriptions", MappingProxyType(dict(self.descriptions)))

    def report_for(self, alias: str) -> PersistedObservationReport | None:
        index = self.aliases.get(alias)
        return None if index is None else self.reports[index]


def build_observation_consumer_inputs(
    reports: tuple[PersistedObservationReport, ...], *, max_prompt_chars: int = 16_384,
) -> ObservationConsumerInputs:
    if type(reports) is not tuple or not reports:
        raise ObservationInputError("reports must be a non-empty tuple")
    if type(max_prompt_chars) is not int or max_prompt_chars < 1:
        raise ObservationInputError("max_prompt_chars must be positive")
    aliases: dict[str, int] = {}
    descriptions: dict[str, str] = {}
    for index, persisted in enumerate(reports):
        if type(persisted) is not PersistedObservationReport:
            raise ObservationInputError("reports must contain exact persisted observation reports")
        if persisted.status != "observation_unverified":
            raise ObservationInputError("observation report is not explicitly unverified")
        alias = _alias(index)
        aliases[alias] = index
        report = persisted.report
        rendered = json.dumps(
            report.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        if len(rendered) > max_prompt_chars:
            raise ObservationInputError("observation report exceeds the explicit prompt budget")
        descriptions[alias] = rendered
    return ObservationConsumerInputs(reports, aliases, descriptions)


def consumer_schema(stage: str, inputs: ObservationConsumerInputs) -> dict[str, object]:
    if type(inputs) is not ObservationConsumerInputs:
        raise ObservationInputError("consumer schema requires exact inputs")
    members = sorted(inputs.aliases)
    reference = {"type": "array", "items": {"enum": members}, "uniqueItems": True}
    if stage == "narrative":
        item = {"type": "object", "additionalProperties": False, "required": ["summary", "report_refs", "interpretations", "uncertainties"], "properties": {"summary": {"type": "string"}, "report_refs": reference, "interpretations": {"type": "array", "items": {"type": "string"}}, "uncertainties": {"type": "array", "items": {"type": "string"}}}}
        return {"type": "object", "additionalProperties": False, "required": ["units"], "properties": {"units": {"type": "array", "items": item}}}
    raise ObservationInputError("only narrative inputs are implemented; story/blueprint need typed predecessors")


def build_consumer_prompt(stage: str, inputs: ObservationConsumerInputs) -> str:
    consumer_schema(stage, inputs)
    directory = [{"id": alias, "observation": json.loads(inputs.descriptions[alias])} for alias in sorted(inputs.aliases)]
    return "仅使用目录中的短引用；所有观察均为未验证的模型陈述，保留歧义，不生成事实、证据、物理素材或 ID。\n" + json.dumps(directory, ensure_ascii=False)


__all__ = ("ObservationConsumerInputs", "ObservationInputError", "build_consumer_prompt", "build_observation_consumer_inputs", "consumer_schema")
