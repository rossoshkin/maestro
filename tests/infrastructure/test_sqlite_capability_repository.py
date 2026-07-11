"""Tests for SQLite Capability persistence."""

import asyncio

import pytest

from maestro.domain import ResourceSelector
from maestro.domain.capabilities import (
    Capability,
    CapabilityBinding,
    CapabilityBindingPhase,
    CapabilityBindingSpec,
    CapabilityBindingStatus,
    CapabilityPhase,
    CapabilityScope,
    CapabilitySideEffectLevel,
    CapabilitySpec,
    CapabilityStatus,
)
from maestro.domain.exceptions import ResourceAlreadyExistsError, ResourceConflictError
from maestro.infrastructure.persistence import (
    SQLiteCapabilityBindingRepository,
    SQLiteCapabilityRepository,
)


def capability_spec(canonical_name: str = "filesystem.read") -> CapabilitySpec:
    """Build a valid CapabilitySpec for persistence tests."""

    schema_name = "".join(part.capitalize() for part in canonical_name.split("."))
    return CapabilitySpec(
        canonicalName=canonical_name,
        description=f"Capability for {canonical_name}",
        sideEffectLevel=CapabilitySideEffectLevel.READ_ONLY,
        scopes=(CapabilityScope.WORKSPACE,),
        inputSchemaRef=f"{schema_name}Input/v1",
        outputSchemaRef=f"{schema_name}Output/v1",
    )


def capability(canonical_name: str = "filesystem.read") -> Capability:
    """Build a Capability resource."""

    return Capability.new(
        name=canonical_name.replace(".", "-"),
        spec=capability_spec(canonical_name),
    )


def binding() -> CapabilityBinding:
    """Build a CapabilityBinding resource."""

    return CapabilityBinding.new(
        name="local-workspace-safe",
        spec=CapabilityBindingSpec(
            grants=("filesystem.read", "filesystem.write"),
            denies=("git.push",),
        ),
    )


def test_capability_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(tmp_path / "maestro.db")
        resource = await repository.create(capability())
        loaded = await repository.get(resource.metadata.id)

        assert loaded == resource
        repository.close()

    asyncio.run(scenario())


def test_capability_persistence_survives_repository_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "maestro.db"
        first_repository = SQLiteCapabilityRepository(database_path)
        resource = await first_repository.create(capability())
        first_repository.close()

        second_repository = SQLiteCapabilityRepository(database_path)
        loaded = await second_repository.get(resource.metadata.id)

        assert loaded.metadata.id == resource.metadata.id
        assert loaded.spec.canonical_name == "filesystem.read"
        second_repository.close()

    asyncio.run(scenario())


def test_capability_lookup_by_canonical_name() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(":memory:")
        resource = await repository.create(capability())

        loaded = await repository.get_by_canonical_name("default", "filesystem.read")

        assert loaded == resource
        repository.close()

    asyncio.run(scenario())


def test_capability_canonical_names_are_unique() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(":memory:")
        await repository.create(capability())

        with pytest.raises(ResourceAlreadyExistsError):
            await repository.create(
                Capability.new(name="filesystem-read-copy", spec=capability_spec())
            )
        repository.close()

    asyncio.run(scenario())


def test_capability_update_status_preserves_generation() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(":memory:")
        resource = await repository.create(capability())

        updated = await repository.update_status(
            resource.metadata.id,
            CapabilityStatus(
                observedGeneration=1,
                phase=CapabilityPhase.READY,
                toolImplementations=("local-filesystem",),
            ),
            expected_resource_version=resource.metadata.resource_version,
        )

        assert updated.metadata.generation == 1
        assert updated.metadata.resource_version == 2
        assert updated.status.phase == CapabilityPhase.READY
        repository.close()

    asyncio.run(scenario())


def test_capability_update_spec_uses_optimistic_concurrency() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(":memory:")
        resource = await repository.create(capability())
        changed_spec = resource.spec.model_copy(update={"description": "Changed"})

        updated = await repository.update_spec(
            resource.metadata.id,
            changed_spec,
            expected_resource_version=resource.metadata.resource_version,
        )

        assert updated.metadata.generation == 2
        assert updated.metadata.resource_version == 2

        with pytest.raises(ResourceConflictError):
            await repository.update_spec(
                resource.metadata.id,
                changed_spec,
                expected_resource_version=resource.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())


def test_capability_repository_lists_by_labels() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityRepository(":memory:")
        resource = capability()
        labeled = resource.model_copy(
            update={
                "metadata": resource.metadata.model_copy(
                    update={"labels": {"side-effect": "read-only"}}
                )
            }
        )
        await repository.create(labeled)

        selected = await repository.list(
            ResourceSelector(labels={"side-effect": "read-only"})
        )

        assert [capability.metadata.name for capability in selected] == [
            "filesystem-read"
        ]
        repository.close()

    asyncio.run(scenario())


def test_capability_binding_persistence_round_trip(tmp_path) -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityBindingRepository(tmp_path / "maestro.db")
        resource = await repository.create(binding())
        loaded = await repository.get(resource.metadata.id)

        assert loaded == resource
        repository.close()

    asyncio.run(scenario())


def test_capability_binding_lists_ready_bindings() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityBindingRepository(":memory:")
        first = await repository.create(binding())
        await repository.create(
            CapabilityBinding.new(
                name="pending",
                spec=CapabilityBindingSpec(grants=("knowledge.search",)),
            )
        )
        await repository.update_status(
            first.metadata.id,
            CapabilityBindingStatus(
                observedGeneration=1,
                phase=CapabilityBindingPhase.READY,
            ),
            expected_resource_version=first.metadata.resource_version,
        )

        ready = await repository.list_ready("default")

        assert [binding.metadata.name for binding in ready] == ["local-workspace-safe"]
        repository.close()

    asyncio.run(scenario())


def test_capability_binding_stale_update_returns_conflict() -> None:
    async def scenario() -> None:
        repository = SQLiteCapabilityBindingRepository(":memory:")
        resource = await repository.create(binding())
        status = CapabilityBindingStatus(phase=CapabilityBindingPhase.READY)

        await repository.update_status(
            resource.metadata.id,
            status,
            expected_resource_version=resource.metadata.resource_version,
        )

        with pytest.raises(ResourceConflictError):
            await repository.update_status(
                resource.metadata.id,
                status,
                expected_resource_version=resource.metadata.resource_version,
            )
        repository.close()

    asyncio.run(scenario())
