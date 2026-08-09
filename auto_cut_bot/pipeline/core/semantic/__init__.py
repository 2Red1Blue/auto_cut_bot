"""autocut_core.semantic — 语义处理模块 (从 semantic_handlers.py 逐步提取)。"""

from __future__ import annotations

from autocut_core.semantic.utils import diagnostic_path, records_by_id
from autocut_core.semantic.registry import (
    _direct_evidence_contract_indexes,
    attempt_series_registry_identity_repair,
    attempt_series_registry_relationship_repair,
    contract_cache_metadata,
    inherit_registry_admission_repairs,
    rebuild_cached_registry_admission,
    record_contract_repairs,
    write_series_assignment_repair_report,
    write_series_registry_alias_repair_report,
    write_series_registry_identity_repair_report,
    write_series_registry_recovery_merge_report,
    write_series_registry_reference_repair_report,
    write_series_registry_relationship_repair_report,
)
from autocut_core.semantic.request import (
    TASK_SKILL_MAP,
    load_skill_for_task,
)
from autocut_core.semantic.batch_runner import (
    _beat_direct_event_ids,
    _entry_symbol,
    run_job,
    semantic_validation_error_summary,
)

__all__ = [
    "diagnostic_path",
    "records_by_id",
    "TASK_SKILL_MAP",
    "load_skill_for_task",
    "_beat_direct_event_ids",
    "_direct_evidence_contract_indexes",
    "_entry_symbol",
    "attempt_series_registry_identity_repair",
    "attempt_series_registry_relationship_repair",
    "contract_cache_metadata",
    "inherit_registry_admission_repairs",
    "rebuild_cached_registry_admission",
    "record_contract_repairs",
    "run_job",
    "semantic_validation_error_summary",
    "write_series_assignment_repair_report",
    "write_series_registry_alias_repair_report",
    "write_series_registry_identity_repair_report",
    "write_series_registry_recovery_merge_report",
    "write_series_registry_reference_repair_report",
    "write_series_registry_relationship_repair_report",
]