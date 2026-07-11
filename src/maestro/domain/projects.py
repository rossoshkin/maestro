"""Project resource models and repository contract."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from maestro.domain.repositories import ResourceRepository
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    ResourceName,
    Spec,
    Status,
)

ReferenceVersion = Annotated[str, Field(min_length=1, max_length=63)]
RoleBindingName = ResourceName
RepositoryId = ResourceName


class RepositoryType(StrEnum):
    """Supported Project repository binding types."""

    GIT = "git"


class ProjectPhase(StrEnum):
    """Project status phases."""

    PENDING = "Pending"
    VALIDATING = "Validating"
    READY = "Ready"
    DEGRADED = "Degraded"
    ARCHIVED = "Archived"
    ERROR = "Error"


class DependencyChangePolicy(StrEnum):
    """Project policy for dependency changes."""

    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval-required"
    DENY = "deny"


class ProjectRepositoryBinding(MaestroModel):
    """Repository configured for a Project."""

    id: RepositoryId
    path: Path
    default_branch: str = Field(min_length=1, alias="defaultBranch")
    type: RepositoryType = RepositoryType.GIT

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        """Reject relative repository paths."""

        if not value.is_absolute():
            raise ValueError("repository path must be absolute")
        return value


class WorkflowReference(MaestroModel):
    """Reference to the Workflow version pinned by a Project."""

    kind: Literal["Workflow"] = "Workflow"
    name: ResourceName
    version: ReferenceVersion


class AgentReference(MaestroModel):
    """Reference to an Agent configured for a Role binding."""

    kind: Literal["Agent"] = "Agent"
    name: ResourceName


class ProjectRoleBinding(MaestroModel):
    """Binding from a logical Role name to an Agent reference."""

    agent_ref: AgentReference = Field(alias="agentRef")


class KnowledgeSourceReference(MaestroModel):
    """Reference to a KnowledgeSource available to a Project."""

    kind: Literal["KnowledgeSource"] = "KnowledgeSource"
    name: ResourceName


class ProjectPolicy(MaestroModel):
    """Safety and approval defaults for a Project."""

    require_plan_approval: bool = Field(default=True, alias="requirePlanApproval")
    require_final_approval: bool = Field(default=True, alias="requireFinalApproval")
    allow_network: bool = Field(default=False, alias="allowNetwork")
    allow_dependency_changes: DependencyChangePolicy = Field(
        default=DependencyChangePolicy.APPROVAL_REQUIRED,
        alias="allowDependencyChanges",
    )


class ProjectSpec(Spec):
    """Desired configuration for a Maestro Project."""

    description: str = ""
    repositories: tuple[ProjectRepositoryBinding, ...] = Field(default_factory=tuple)
    workflow_ref: WorkflowReference = Field(alias="workflowRef")
    role_bindings: dict[RoleBindingName, ProjectRoleBinding] = Field(
        default_factory=dict,
        alias="roleBindings",
    )
    knowledge_bindings: tuple[KnowledgeSourceReference, ...] = Field(
        default_factory=tuple,
        alias="knowledgeBindings",
    )
    policies: ProjectPolicy = Field(default_factory=ProjectPolicy)
    archived: bool = False

    @field_validator("repositories")
    @classmethod
    def reject_duplicate_repository_ids(
        cls,
        value: tuple[ProjectRepositoryBinding, ...],
    ) -> tuple[ProjectRepositoryBinding, ...]:
        """Reject duplicate repository IDs in one Project."""

        repository_ids = [repository.id for repository in value]
        if len(set(repository_ids)) != len(repository_ids):
            raise ValueError("repository IDs must be unique within a Project")
        return value

    @field_validator("knowledge_bindings")
    @classmethod
    def reject_duplicate_knowledge_bindings(
        cls,
        value: tuple[KnowledgeSourceReference, ...],
    ) -> tuple[KnowledgeSourceReference, ...]:
        """Reject duplicate KnowledgeSource bindings."""

        names = [binding.name for binding in value]
        if len(set(names)) != len(names):
            raise ValueError("knowledge bindings must be unique within a Project")
        return value


class ProjectRepositoryStatus(MaestroModel):
    """Observed repository state for a Project."""

    id: RepositoryId
    reachable: bool
    git_repository: bool = Field(alias="gitRepository")
    clean: bool
    head_revision: str | None = Field(default=None, alias="headRevision")


class ProjectStatus(Status):
    """Observed state for a Maestro Project."""

    phase: ProjectPhase = ProjectPhase.PENDING
    repositories: tuple[ProjectRepositoryStatus, ...] = Field(default_factory=tuple)

    @field_validator("repositories")
    @classmethod
    def reject_duplicate_repository_statuses(
        cls,
        value: tuple[ProjectRepositoryStatus, ...],
    ) -> tuple[ProjectRepositoryStatus, ...]:
        """Reject duplicate repository status entries."""

        repository_ids = [repository.id for repository in value]
        if len(set(repository_ids)) != len(repository_ids):
            raise ValueError("repository statuses must be unique by repository ID")
        return value


class Project(BaseResource[ProjectSpec, ProjectStatus]):
    """Root configuration resource for a Maestro Project."""

    kind: Literal["Project"] = "Project"

    @model_validator(mode="after")
    def validate_project_metadata(self) -> Self:
        """Ensure Project metadata is consistent with Project semantics."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.controller:
                raise ValueError("Project resources cannot have controller owners")
        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ProjectSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Project resource with initialized metadata and status."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=ProjectStatus(),
        )


class ProjectRepository(
    ResourceRepository[Project, ProjectSpec, ProjectStatus],
    Protocol,
):
    """Persistence contract for Project resources."""

    async def get_by_name(self, namespace: str, name: str) -> Project:
        """Load a Project by namespace and name."""

    async def mark_deleted(
        self,
        resource_id: UUID,
        *,
        expected_resource_version: int,
    ) -> Project:
        """Mark a Project for deletion without deleting repository contents."""
