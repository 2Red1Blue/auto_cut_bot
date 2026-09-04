"""Independent acceptance of one production-QC collector capability.

The command is deliberately separate from every Render/QC attempt: a
machine-local measurement can never authorize the evidence it produced.  The
only write surface is the protected Store owner API, so a denied or failed
validator command owns no ArtifactSet, member or capability row.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes
from ..registry.installed_production_qc import (
    InstalledProductionQcResource,
)
from ..rendering.production_qc_collector_capability import (
    ProductionQcCollectorCapabilityError,
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorLiveProfile,
)
from ..store import (
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    IdempotencyConflictError,
    PostgresRuntimeStore,
    ProductionQcCollectorCapabilityBinding,
    RuntimeStoreError,
    StoreValidationError,
)


class AcceptProductionQcCollectorCapabilityError(ValueError):
    """A collector capability acceptance input is invalid or inconsistent."""


class AcceptProductionRenderQcCollectorCapabilityCommand:
    """Accept one freshly measured collector under the installed static policy."""

    def __init__(self, store: PostgresRuntimeStore) -> None:
        self.store = store

    def execute(
        self,
        resource: InstalledProductionQcResource,
        live_profile: ProductionQcCollectorLiveProfile,
    ) -> CommandOutcome:
        try:
            request = ProductionQcCollectorCapabilityRequest(
                resource.policy,
                live_profile,
            )
        except ProductionQcCollectorCapabilityError as error:
            raise AcceptProductionQcCollectorCapabilityError(
                f"live measurement does not match the installed static policy: {error}"
            ) from error
        binding = ProductionQcCollectorCapabilityBinding(request, resource.provenance)
        claimed = self.store.claim_qc_collector_capability_command(binding.claim)
        if not claimed.is_fresh_claim:
            return claimed
        try:
            committed = self.store.commit_qc_collector_capability_success(
                CommandSuccess(
                    claimed.command_slot_id,
                    binding.expected_set_hash,
                    binding.members,
                ),
                binding,
            )
            return replace(committed, is_fresh_claim=True)
        except IdempotencyConflictError:
            # A conflict means the durable identity already decided differently;
            # it must surface instead of being rewritten into a terminal receipt.
            raise
        except StoreValidationError as error:
            return self._reject(claimed.command_slot_id, str(error), "denied")
        except (RuntimeStoreError, OSError) as error:
            return self._reject(claimed.command_slot_id, type(error).__name__, "failed")
        # Commit failures/ambiguous outcomes are not rewritten into rejection:
        # the next call must reconcile/replay the Store's authoritative receipt.

    def _reject(self, command_slot_id: UUID, reason: str, outcome: str) -> CommandOutcome:
        detail = canonical_json_bytes(
            {
                "reason": reason,
                "stage": "production_qc_collector_capability",
            }
        ).decode("utf-8")
        return self.store.commit_command_rejection(
            CommandRejection(
                command_slot_id,
                "PRODUCTION_QC_COLLECTOR_CAPABILITY_DENIED"
                if outcome == "denied"
                else "PRODUCTION_QC_COLLECTOR_CAPABILITY_INDETERMINATE",
                detail,
                outcome,  # pyright: ignore[reportArgumentType]
            )
        )
