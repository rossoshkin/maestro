"""Persistence adapters for Maestro resources."""

from maestro.infrastructure.persistence.sqlite_agent_repository import (
    SQLiteAgentRepository,
)
from maestro.infrastructure.persistence.sqlite_approval_repository import (
    SQLiteApprovalRepository,
)
from maestro.infrastructure.persistence.sqlite_artifact_repository import (
    SQLiteArtifactRepository,
)
from maestro.infrastructure.persistence.sqlite_capability_repository import (
    SQLiteCapabilityBindingRepository,
    SQLiteCapabilityRepository,
)
from maestro.infrastructure.persistence.sqlite_event_store import SQLiteEventStore
from maestro.infrastructure.persistence.sqlite_execution_repository import (
    SQLiteExecutionRepository,
)
from maestro.infrastructure.persistence.sqlite_plan_repository import (
    SQLitePlanRepository,
)
from maestro.infrastructure.persistence.sqlite_project_repository import (
    SQLiteProjectRepository,
)
from maestro.infrastructure.persistence.sqlite_provider_repository import (
    SQLiteProviderRepository,
)
from maestro.infrastructure.persistence.sqlite_review_repository import (
    SQLiteReviewRepository,
)
from maestro.infrastructure.persistence.sqlite_role_invocation_repository import (
    SQLiteRoleInvocationRepository,
)
from maestro.infrastructure.persistence.sqlite_role_repository import (
    SQLiteRoleRepository,
)
from maestro.infrastructure.persistence.sqlite_work_item_repository import (
    SQLiteWorkItemRepository,
)
from maestro.infrastructure.persistence.sqlite_workflow_repository import (
    SQLiteWorkflowRepository,
)
from maestro.infrastructure.persistence.sqlite_workspace_repository import (
    SQLiteWorkspaceRepository,
)

__all__ = [
    "SQLiteAgentRepository",
    "SQLiteApprovalRepository",
    "SQLiteArtifactRepository",
    "SQLiteCapabilityBindingRepository",
    "SQLiteCapabilityRepository",
    "SQLiteEventStore",
    "SQLiteExecutionRepository",
    "SQLitePlanRepository",
    "SQLiteProjectRepository",
    "SQLiteProviderRepository",
    "SQLiteReviewRepository",
    "SQLiteRoleInvocationRepository",
    "SQLiteRoleRepository",
    "SQLiteWorkItemRepository",
    "SQLiteWorkspaceRepository",
    "SQLiteWorkflowRepository",
]
