"""Tests for SQLite Role persistence."""

import asyncio

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.exceptions import (
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceImmutableFieldError,
)
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePhase,
    RolePromptReference,
    RoleSpec,
    RoleStatus,
    RoleValidationResult,
)
from maestro.infrastructure.persistence import SQLiteRoleRepository


def valid_role_spec(*, version: str = "v1alpha1") -> RoleSpec:
    """Build a valid RoleSpec for persistence tests."""

    return RoleSpec(
        version=version,
        purpose="Implement one software Work Item",
        inputSchemaRef="CodingInput/v1",
        outputSchemaRef="CodingResult/v1",
        promptRef=RolePromptReference(name="coding-prompt-v1", version=version),
        requiredCapabilities=(
            "filesystem.read",
            "filesystem.write",
            "filesystem.edit",
            "git.status",
            "git.diff",
        ),
        optionalCapabilities=("shell.execute.test", "knowledge.search"),
        prohibitedCapabilities=(
            "git.push",
            "deployment.execute",
            "workflow.transition",
        ),
        executionPolicy=RoleExecutionPolicy(maxSteps=40),
    )


def test_role_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(tmp_path / "maestro.db")
        role = await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        loaded = await repository.get(role.metadata.id)

        assert loaded == role
        repository.close()

    asyncio.run(scenario())


def test_role_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteRoleRepository(database_path)
        role = await first_repository.create(
            Role.new(name="coding", spec=valid_role_spec())
        )
        first_repository.close()

        second_repository = SQLiteRoleRepository(database_path)
        loaded = await second_repository.get(role.metadata.id)

        assert loaded.metadata.id == role.metadata.id
        assert loaded.spec.version == "v1alpha1"
        second_repository.close()

    asyncio.run(scenario())


def test_role_lookup_by_exact_name_and_version() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        role = await repository.create(Role.new(name="coding", spec=valid_role_spec()))

        loaded = await repository.get_by_name_version(
            role.metadata.namespace,
            role.metadata.name,
            role.spec.version,
        )

        assert loaded == role
        repository.close()

    asyncio.run(scenario())


def test_role_versions_are_unique_by_name_and_version() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        await repository.create(Role.new(name="coding", spec=valid_role_spec()))

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        repository.close()

    asyncio.run(scenario())


def test_role_new_version_can_be_registered() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        next_role = await repository.create(
            Role.new(name="coding", spec=valid_role_spec(version="v1alpha2"))
        )

        assert next_role.spec.version == "v1alpha2"
        repository.close()

    asyncio.run(scenario())


def test_role_spec_updates_are_rejected() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        role = await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        changed_spec = role.spec.model_copy(update={"purpose": "Changed"})

        with pytest.raises(ResourceImmutableFieldError):
            await repository.update_spec(
                role.metadata.id,
                changed_spec,
                expected_resource_version=role.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_role_status_update_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        role = await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        status = RoleStatus(
            observedGeneration=1,
            phase=RolePhase.READY,
            validation=RoleValidationResult(valid=True),
        )

        updated = await repository.update_status(
            role.metadata.id,
            status,
            expected_resource_version=role.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == RolePhase.READY
        repository.close()

    asyncio.run(scenario())


def test_role_stale_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        role = await repository.create(Role.new(name="coding", spec=valid_role_spec()))
        status = RoleStatus(phase=RolePhase.READY)

        await repository.update_status(
            role.metadata.id,
            status,
            expected_resource_version=role.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                role.metadata.id,
                status,
                expected_resource_version=role.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_role_repository_lists_by_namespace_and_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteRoleRepository(":memory:")
        role = Role.new(name="coding", spec=valid_role_spec())
        labeled_role = role.model_copy(
            update={
                "metadata": role.metadata.model_copy(
                    update={"labels": {"domain": "software"}}
                )
            }
        )
        await repository.create(labeled_role)

        selected = await repository.list(
            ResourceSelector(labels={"domain": "software"})
        )

        assert [role.metadata.name for role in selected] == ["coding"]
        repository.close()

    asyncio.run(scenario())
