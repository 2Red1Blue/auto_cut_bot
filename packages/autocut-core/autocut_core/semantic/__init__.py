"""autocut_core.semantic — 共享语义处理模块 (数据准备、合同、融合)。"""

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

__all__ = [
    "diagnostic_path",
    "records_by_id",
    "_direct_evidence_contract_indexes",
    "attempt_series_registry_identity_repair",
    "attempt_series_registry_relationship_repair",
    "contract_cache_metadata",
    "inherit_registry_admission_repairs",
    "rebuild_cached_registry_admission",
    "record_contract_repairs",
    "write_series_assignment_repair_report",
    "write_series_registry_alias_repair_report",
    "write_series_registry_identity_repair_report",
    "write_series_registry_recovery_merge_report",
    "write_series_registry_reference_repair_report",
    "write_series_registry_relationship_repair_report",
]
