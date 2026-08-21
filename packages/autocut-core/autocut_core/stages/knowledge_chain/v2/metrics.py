"""知识链v2指标统计模块"""

import json
import logging

from .types import JSONObject, JSONValue

logger = logging.getLogger(__name__)


class MetricsCollector:
    def __init__(self) -> None:
        self.data: JSONObject = {
            "total_llm_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "chapter_count": 0,
            "core_character_count": 0,
            "primary_thread_count": 0,
            "unassigned_event_count": 0,
            "new_character_count": 0,
            "duplicate_entity_count": 0,
            "warning_count": 0,
            "id_error_rate": 0.0,
            "fragmentation_rate": 0.0,
        }

    @staticmethod
    def _integer(value: JSONValue | None) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def add_llm_call(self, input_tokens: int, output_tokens: int) -> None:
        self.data["total_llm_calls"] = self._integer(self.data.get("total_llm_calls")) + 1
        self.data["total_input_tokens"] = (
            self._integer(self.data.get("total_input_tokens")) + input_tokens
        )
        self.data["total_output_tokens"] = (
            self._integer(self.data.get("total_output_tokens")) + output_tokens
        )

    def generate_report(self) -> JSONObject:
        report = self.data.copy()
        report["total_tokens"] = self._integer(report.get("total_input_tokens")) + self._integer(
            report.get("total_output_tokens")
        )
        return report

    def log_report(self) -> None:
        report = self.generate_report()
        logger.info("=== Knowledge Chain v2 Metrics Report ===")
        logger.info(json.dumps(report, indent=2, ensure_ascii=False))
