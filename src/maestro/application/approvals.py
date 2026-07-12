"""Application services for Approval decisions and invalidation."""

from typing import Any
from uuid import UUID

from maestro.domain.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalRepository,
    invalidated_approval_status_for_subject,
    record_approval_decision,
)
from maestro.domain.resources import BaseResource


class ApprovalService:
    """Coordinate Approval decision history and subject invalidation."""

    def __init__(self, approval_repository: ApprovalRepository) -> None:
        self._approval_repository = approval_repository

    async def record_decision(
        self,
        resource_id: UUID,
        decision: ApprovalDecision,
        *,
        expected_resource_version: int,
    ) -> Approval:
        """Append an attributable decision to an Approval."""

        approval = await self._approval_repository.get(resource_id)
        updated = record_approval_decision(
            approval,
            decision,
            expected_resource_version=expected_resource_version,
        )
        return await self._approval_repository.update_status(
            resource_id,
            updated.status,
            expected_resource_version=expected_resource_version,
        )

    async def invalidate_if_subject_changed(
        self,
        resource_id: UUID,
        subject: BaseResource[Any, Any],
        *,
        expected_resource_version: int,
    ) -> Approval:
        """Invalidate an Approval when its referenced subject version changed."""

        approval = await self._approval_repository.get(resource_id)
        invalidated = invalidated_approval_status_for_subject(approval, subject)
        if invalidated is None:
            return approval

        return await self._approval_repository.update_status(
            resource_id,
            invalidated,
            expected_resource_version=expected_resource_version,
        )
