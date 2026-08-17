"""语义模块共享类型 — 从 semantic_handlers.py 提取的 dataclass 和异常类。

这是 semantic_handlers.py 中最后 5 个非 shim 项, 被所有新模块引用。
提取到独立模块后, 新模块不再需要反向 import semantic_handlers。

合约结果类型已迁入 contracts.py。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AssignmentContractResult,
    GlobalAssignmentGraphResult,
    RegistryRecoveryMergeResult,
    SeriesRegistryAdmissionResult,
    SeriesRegistryAliasRepairResult,
    SeriesRegistryIdentityRepairResult,
    SeriesRegistryReferenceRepairResult,
    SeriesRegistryRelationshipRepairResult,
    WindowAnalysisContractResult,
)


class StoryScriptCompileReplacementResult:
    """Deterministic result of applying a compile-only Beat fragment."""

    value: dict[str, Any]
    errors: list[str]
    audit: dict[str, Any]


class StoryScriptAdmissionResult:
    """Pure local preflight verdict used before draft output/cache admission."""

    feasibility_status: str
    failure_codes: list[str]
    repair_route: str
    errors: list[str]
    treatment_viability: dict[str, Any] | None = None
    split_regeneration_beat_ids: tuple[str, ...] = ()


class StoryScriptSemanticRejection(ValueError):
    """An exhausted Story Script job that may be isolated from its siblings."""

    def __init__(
        self,
        message: str,
        *,
        story_id: str,
        failure_codes: list[str],
        repair_route: str = "story_script",
        failure_class: str = "semantic",
        compile_repair_stop_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.story_id = story_id
        self.failure_codes = list(dict.fromkeys(failure_codes))
        self.repair_route = repair_route
        self.failure_class = failure_class
        self.compile_repair_stop_reason = compile_repair_stop_reason


class WindowAnalysisSemanticRejection(ValueError):
    """An exhausted Window response with a typed, bounded recovery route."""

    def __init__(
        self,
        message: str,
        *,
        failure_codes: list[str],
        blockers: list[dict[str, Any]],
        repair_route: str,
        media_recovery_attempted: bool,
        request_signature: str,
    ) -> None:
        super().__init__(message)
        self.failure_codes = list(dict.fromkeys(failure_codes))
        self.blockers = copy.deepcopy(blockers)
        self.repair_route = repair_route
        self.media_recovery_attempted = media_recovery_attempted
        self.request_signature = request_signature


@dataclass
class JobResponseValidation:
    """Aggregated validation result after schema/identity/contract checks."""

    value: dict[str, Any]
    schema_errors: list[str]
    identity_errors: list[str]
    contract_errors: list[str]
    contract_result: AssignmentContractResult | None = None
    window_contract_result: WindowAnalysisContractResult | None = None
    registry_repair_result: SeriesRegistryRelationshipRepairResult | None = None
    registry_recovery_merge_result: RegistryRecoveryMergeResult | None = None
    registry_alias_repair_result: SeriesRegistryAliasRepairResult | None = None
    registry_reference_repair_result: SeriesRegistryReferenceRepairResult | None = None
    registry_admission_result: SeriesRegistryAdmissionResult | None = None
    registry_identity_repair_result: SeriesRegistryIdentityRepairResult | None = None
    story_script_admission: StoryScriptAdmissionResult | None = None
    event_id_truncation_repairs: list[dict[str, Any]] | None = None

    @property
    def errors(self) -> list[str]:
        return [
            *self.schema_errors,
            *self.identity_errors,
            *self.contract_errors,
        ]

    @property
    def local_repairs(self) -> list[dict[str, Any]]:
        repairs: list[dict[str, Any]] = []
        if self.registry_alias_repair_result is not None:
            repairs.extend(self.registry_alias_repair_result.repairs)
        if self.registry_reference_repair_result is not None:
            repairs.extend(self.registry_reference_repair_result.repairs)
        if self.registry_identity_repair_result is not None:
            repairs.extend(self.registry_identity_repair_result.repairs)
        if self.registry_admission_result is not None:
            repairs.extend(self.registry_admission_result.repairs)
        if self.registry_repair_result is not None:
            repairs.extend(self.registry_repair_result.repairs)
        if self.registry_recovery_merge_result is not None:
            repairs.extend(self.registry_recovery_merge_result.imported_relationships)
        if self.event_id_truncation_repairs:
            repairs.extend(self.event_id_truncation_repairs)
        if repairs:
            return repairs
        if self.window_contract_result is not None:
            return self.window_contract_result.repairs
        if self.contract_result is not None:
            return self.contract_result.repairs
        return []

    @property
    def quality_status(self) -> str:
        if self.errors:
            return "blocked"
        if (
            self.registry_admission_result is not None
            and self.registry_admission_result.status == "partially_ready"
        ):
            return self.registry_admission_result.status
        if self.window_contract_result is not None:
            return self.window_contract_result.quality_status
        if self.registry_repair_result is not None:
            return "repaired"
        if self.registry_recovery_merge_result is not None:
            return "repaired"
        if self.registry_identity_repair_result is not None:
            return "repaired"
        return "repaired" if self.local_repairs else "valid"