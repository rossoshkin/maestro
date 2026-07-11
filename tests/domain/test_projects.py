"""Tests for the Project resource model."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.domain.projects import (
    AgentReference,
    DependencyChangePolicy,
    KnowledgeSourceReference,
    Project,
    ProjectPolicy,
    ProjectRepositoryBinding,
    ProjectRoleBinding,
    ProjectSpec,
    RepositoryType,
    WorkflowReference,
)


def valid_project_spec(repository_path: Path) -> ProjectSpec:
    """Build a valid ProjectSpec for tests."""

    return ProjectSpec(
        description="Test project",
        repositories=(
            ProjectRepositoryBinding(
                id="backend",
                path=repository_path,
                defaultBranch="main",
                type=RepositoryType.GIT,
            ),
        ),
        workflowRef=WorkflowReference(name="software-delivery", version="v1alpha1"),
        roleBindings={
            "planner": ProjectRoleBinding(
                agentRef=AgentReference(name="planner-local")
            ),
            "coding": ProjectRoleBinding(agentRef=AgentReference(name="coder-local")),
            "reviewer": ProjectRoleBinding(
                agentRef=AgentReference(name="codex-reviewer")
            ),
        },
        knowledgeBindings=(KnowledgeSourceReference(name="project-docs"),),
        policies=ProjectPolicy(
            requirePlanApproval=True,
            requireFinalApproval=True,
            allowNetwork=False,
            allowDependencyChanges=DependencyChangePolicy.APPROVAL_REQUIRED,
        ),
    )


def test_project_serializes_and_deserializes(tmp_path: Path) -> None:
    project = Project.new(name="tour-manager", spec=valid_project_spec(tmp_path))

    payload = project.model_dump(mode="json", by_alias=True)
    round_tripped = Project.model_validate(payload)

    assert payload["kind"] == "Project"
    assert payload["spec"]["workflowRef"]["name"] == "software-delivery"
    assert round_tripped == project


def test_duplicate_repository_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ProjectSpec(
            repositories=(
                ProjectRepositoryBinding(
                    id="backend",
                    path=tmp_path / "backend",
                    defaultBranch="main",
                ),
                ProjectRepositoryBinding(
                    id="backend",
                    path=tmp_path / "backend-copy",
                    defaultBranch="main",
                ),
            ),
            workflowRef=WorkflowReference(name="software-delivery", version="v1alpha1"),
        )


def test_relative_repository_paths_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectRepositoryBinding(
            id="backend",
            path=Path("relative/backend"),
            defaultBranch="main",
        )


def test_missing_workflow_binding_is_rejected(tmp_path: Path) -> None:
    payload = {
        "repositories": [
            {
                "id": "backend",
                "path": str(tmp_path / "backend"),
                "defaultBranch": "main",
                "type": "git",
            }
        ],
        "roleBindings": {},
        "knowledgeBindings": [],
        "policies": {},
    }

    with pytest.raises(ValidationError):
        ProjectSpec.model_validate(payload)


def test_duplicate_knowledge_bindings_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ProjectSpec(
            repositories=(),
            workflowRef=WorkflowReference(name="software-delivery", version="v1alpha1"),
            knowledgeBindings=(
                KnowledgeSourceReference(name="project-docs"),
                KnowledgeSourceReference(name="project-docs"),
            ),
        )


def test_project_cannot_have_controller_owner(tmp_path: Path) -> None:
    payload = Project.new(
        name="tour-manager",
        spec=valid_project_spec(tmp_path),
    ).model_dump(mode="json", by_alias=True)
    payload["metadata"]["ownerReferences"] = [
        {
            "apiVersion": "maestro.dev/v1alpha1",
            "kind": "Execution",
            "id": "00000000-0000-0000-0000-000000000000",
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]

    with pytest.raises(ValidationError):
        Project.model_validate(payload)
