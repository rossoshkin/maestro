"""Tests for deterministic WorkItem scheduling."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from maestro.application.scheduler import SchedulingReason, WorkItemScheduler
from maestro.domain.agents import (
    Agent,
    AgentCapabilityBindingReference,
    AgentCapacity,
    AgentPhase,
    AgentProviderReference,
    AgentScheduling,
    AgentSpec,
    AgentStatus,
    AgentSupportedRole,
)
from maestro.domain.capabilities import (
    Capability,
    CapabilityApprovalPolicy,
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
from maestro.domain.events import EventDraft
from maestro.domain.providers import (
    Provider,
    ProviderDataPolicy,
    ProviderPhase,
    ProviderSpec,
    ProviderStatus,
)
from maestro.domain.roles import (
    Role,
    RoleExecutionPolicy,
    RolePhase,
    RoleSpec,
    RoleStatus,
    RoleValidationResult,
)
from maestro.domain.work_items import (
    WorkItem,
    WorkItemExecutionReference,
    WorkItemPhase,
    WorkItemPlanReference,
    WorkItemRoleReference,
    WorkItemSpec,
    WorkItemStatus,
    WorkItemVerificationSpec,
)
from maestro.infrastructure.persistence import (
    SQLiteAgentRepository,
    SQLiteCapabilityBindingRepository,
    SQLiteCapabilityRepository,
    SQLiteProviderRepository,
    SQLiteRoleRepository,
    SQLiteWorkItemRepository,
)


class RecordingPublisher:
    """Capture scheduler audit events."""

    def __init__(self) -> None:
        self.events: list[EventDraft] = []

    async def publish(self, draft: EventDraft) -> object:
        self.events.append(draft)
        return object()


def ready_role(
    *,
    name: str = "coding",
    version: str = "v1alpha1",
    required_capabilities: tuple[str, ...] = ("filesystem.read",),
) -> Role:
    role = Role.new(
        name=name,
        spec=RoleSpec(
            version=version,
            purpose=f"{name} role",
            inputSchemaRef="WorkItemInput/v1",
            outputSchemaRef="WorkItemOutput/v1",
            requiredCapabilities=required_capabilities,
            executionPolicy=RoleExecutionPolicy(maxSteps=20),
        ),
    )
    return Role(
        metadata=role.metadata,
        spec=role.spec,
        status=RoleStatus(
            observedGeneration=role.metadata.generation,
            phase=RolePhase.READY,
            validation=RoleValidationResult(valid=True),
        ),
    )


def ready_capability(
    canonical_name: str,
    *,
    side_effect_level: CapabilitySideEffectLevel = CapabilitySideEffectLevel.READ_ONLY,
) -> Capability:
    schema_name = "".join(part.capitalize() for part in canonical_name.split("."))
    capability = Capability.new(
        name=canonical_name.replace(".", "-"),
        spec=CapabilitySpec(
            canonicalName=canonical_name,
            description=f"Capability for {canonical_name}",
            sideEffectLevel=side_effect_level,
            approvalPolicy=CapabilityApprovalPolicy.NONE,
            scopes=(CapabilityScope.WORKSPACE,),
            inputSchemaRef=f"{schema_name}Input/v1",
            outputSchemaRef=f"{schema_name}Output/v1",
        ),
    )
    return Capability(
        metadata=capability.metadata,
        spec=capability.spec,
        status=CapabilityStatus(
            observedGeneration=capability.metadata.generation,
            phase=CapabilityPhase.READY,
            toolImplementations=("local-tool",),
        ),
    )


def ready_binding(
    *,
    name: str = "local-workspace-safe",
    grants: tuple[str, ...] = ("filesystem.read",),
    denies: tuple[str, ...] = (),
) -> CapabilityBinding:
    binding = CapabilityBinding.new(
        name=name,
        spec=CapabilityBindingSpec(grants=grants, denies=denies),
    )
    return CapabilityBinding(
        metadata=binding.metadata,
        spec=binding.spec,
        status=CapabilityBindingStatus(
            observedGeneration=binding.metadata.generation,
            phase=CapabilityBindingPhase.READY,
        ),
    )


def provider(
    *,
    name: str = "ollama-local",
    model: str = "custom-coder:latest",
    phase: ProviderPhase = ProviderPhase.READY,
) -> Provider:
    resource = Provider.new(
        name=name,
        spec=ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=(model,),
            dataPolicy=ProviderDataPolicy(allowSourceCode=True),
        ),
    )
    return Provider(
        metadata=resource.metadata,
        spec=resource.spec,
        status=ProviderStatus(
            observedGeneration=resource.metadata.generation,
            phase=phase,
            availableModels=(model,) if phase == ProviderPhase.READY else (),
        ),
    )


def agent(
    *,
    name: str,
    provider_name: str = "ollama-local",
    model: str = "custom-coder:latest",
    role_name: str = "coding",
    role_version: str = "v1alpha1",
    priority: int = 100,
    current_assignments: int = 0,
    max_assignments: int = 1,
) -> Agent:
    resource = Agent.new(
        name=name,
        spec=AgentSpec(
            providerRef=AgentProviderReference(name=provider_name),
            model=model,
            supportedRoles=(
                AgentSupportedRole(name=role_name, versions=(role_version,)),
            ),
            capabilityBindings=(
                AgentCapabilityBindingReference(name="local-workspace-safe"),
            ),
            capacity=AgentCapacity(maxConcurrentAssignments=max_assignments),
            scheduling=AgentScheduling(priority=priority),
        ),
    )
    return Agent(
        metadata=resource.metadata,
        spec=resource.spec,
        status=AgentStatus(
            observedGeneration=resource.metadata.generation,
            phase=(
                AgentPhase.BUSY
                if current_assignments >= max_assignments
                else AgentPhase.READY
            ),
            currentAssignments=current_assignments,
            modelAvailable=True,
        ),
    )


def work_item(
    *,
    execution_id: UUID | None = None,
    role_name: str = "coding",
    role_version: str = "v1alpha1",
    requested_capabilities: tuple[str, ...] = (),
) -> WorkItem:
    return WorkItem.new(
        name=f"{role_name}-work",
        spec=WorkItemSpec(
            executionRef=WorkItemExecutionReference(
                id=execution_id or uuid4(),
                name="add-health-endpoint",
            ),
            planRef=WorkItemPlanReference(id=uuid4(), name="plan-1", version=1),
            planWorkItemId=f"{role_name}-work",
            roleRef=WorkItemRoleReference(name=role_name, version=role_version),
            repositoryRef="backend",
            objective="Implement the assigned change",
            acceptanceCriteria=("Change satisfies the goal",),
            verification=WorkItemVerificationSpec(commands=()),
            requestedCapabilities=requested_capabilities,
        ),
    )


async def ready_work_item(
    repository: SQLiteWorkItemRepository,
    item: WorkItem,
) -> WorkItem:
    created = await repository.create(item)
    return await repository.update_status(
        created.metadata.id,
        WorkItemStatus(
            observedGeneration=created.metadata.generation,
            phase=WorkItemPhase.READY,
        ),
        expected_resource_version=created.metadata.resource_version,
    )


def scheduler(
    *,
    work_item_repository: SQLiteWorkItemRepository,
    agent_repository: SQLiteAgentRepository,
    role_repository: SQLiteRoleRepository,
    provider_repository: SQLiteProviderRepository,
    capability_repository: SQLiteCapabilityRepository,
    binding_repository: SQLiteCapabilityBindingRepository,
    publisher: RecordingPublisher | None = None,
) -> WorkItemScheduler:
    return WorkItemScheduler(
        work_item_repository=work_item_repository,
        agent_repository=agent_repository,
        role_repository=role_repository,
        provider_repository=provider_repository,
        capability_repository=capability_repository,
        capability_binding_repository=binding_repository,
        event_publisher=publisher,
    )


def test_scheduler_assigns_highest_priority_eligible_agent_and_logs_event() -> None:
    async def scenario() -> None:
        work_items = SQLiteWorkItemRepository(":memory:")
        agents = SQLiteAgentRepository(":memory:")
        roles = SQLiteRoleRepository(":memory:")
        providers = SQLiteProviderRepository(":memory:")
        capabilities = SQLiteCapabilityRepository(":memory:")
        bindings = SQLiteCapabilityBindingRepository(":memory:")
        publisher = RecordingPublisher()

        await roles.create(ready_role())
        await providers.create(provider())
        await capabilities.create(ready_capability("filesystem.read"))
        await bindings.create(ready_binding())
        low_priority = await agents.create(agent(name="coder-b", priority=10))
        high_priority = await agents.create(agent(name="coder-a", priority=100))
        item = await ready_work_item(work_items, work_item())

        decision = await scheduler(
            work_item_repository=work_items,
            agent_repository=agents,
            role_repository=roles,
            provider_repository=providers,
            capability_repository=capabilities,
            binding_repository=bindings,
            publisher=publisher,
        ).schedule_work_item(item.metadata.id)

        scheduled = await work_items.get(item.metadata.id)
        selected = await agents.get(high_priority.metadata.id)
        unselected = await agents.get(low_priority.metadata.id)

        assert decision.scheduled is True
        assert decision.assigned_agent_ref == scheduled.status.assigned_agent_ref
        assert scheduled.status.phase == WorkItemPhase.SCHEDULED
        assert scheduled.status.assigned_agent_ref is not None
        assert scheduled.status.assigned_agent_ref.name == "coder-a"
        assert selected.status.current_assignments == 1
        assert selected.status.phase == AgentPhase.BUSY
        assert unselected.status.current_assignments == 0
        assert publisher.events[0].event_type == "WorkItemScheduled"
        assert publisher.events[0].payload["reason"] == SchedulingReason.ASSIGNED

        work_items.close()
        agents.close()
        roles.close()
        providers.close()
        capabilities.close()
        bindings.close()

    asyncio.run(scenario())


def test_scheduler_avoids_unhealthy_and_full_agents_deterministically() -> None:
    async def scenario() -> None:
        work_items = SQLiteWorkItemRepository(":memory:")
        agents = SQLiteAgentRepository(":memory:")
        roles = SQLiteRoleRepository(":memory:")
        providers = SQLiteProviderRepository(":memory:")
        capabilities = SQLiteCapabilityRepository(":memory:")
        bindings = SQLiteCapabilityBindingRepository(":memory:")

        await roles.create(ready_role())
        await providers.create(
            provider(name="bad-provider", phase=ProviderPhase.UNAVAILABLE)
        )
        await providers.create(provider(name="full-provider"))
        await providers.create(provider(name="healthy-provider"))
        await capabilities.create(ready_capability("filesystem.read"))
        await bindings.create(ready_binding())
        await agents.create(
            agent(name="bad-provider-agent", provider_name="bad-provider", priority=300)
        )
        await agents.create(
            agent(
                name="full-agent",
                provider_name="full-provider",
                priority=200,
                current_assignments=1,
                max_assignments=1,
            )
        )
        healthy = await agents.create(
            agent(name="healthy-agent", provider_name="healthy-provider", priority=10)
        )
        item = await ready_work_item(work_items, work_item())

        decision = await scheduler(
            work_item_repository=work_items,
            agent_repository=agents,
            role_repository=roles,
            provider_repository=providers,
            capability_repository=capabilities,
            binding_repository=bindings,
        ).schedule_work_item(item.metadata.id)

        scheduled = await work_items.get(item.metadata.id)
        selected = await agents.get(healthy.metadata.id)
        rejection_reasons = {
            reason
            for evaluation in decision.evaluations
            for reason in evaluation.reasons
        }

        assert scheduled.status.assigned_agent_ref is not None
        assert scheduled.status.assigned_agent_ref.name == "healthy-agent"
        assert selected.status.current_assignments == 1
        assert SchedulingReason.PROVIDER_NOT_READY in rejection_reasons
        assert SchedulingReason.AGENT_AT_CAPACITY in rejection_reasons

        work_items.close()
        agents.close()
        roles.close()
        providers.close()
        capabilities.close()
        bindings.close()

    asyncio.run(scenario())


def test_scheduler_blocks_when_only_incompatible_agents_exist() -> None:
    async def scenario() -> None:
        work_items = SQLiteWorkItemRepository(":memory:")
        agents = SQLiteAgentRepository(":memory:")
        roles = SQLiteRoleRepository(":memory:")
        providers = SQLiteProviderRepository(":memory:")
        capabilities = SQLiteCapabilityRepository(":memory:")
        bindings = SQLiteCapabilityBindingRepository(":memory:")
        publisher = RecordingPublisher()

        await roles.create(ready_role(name="coding"))
        await providers.create(provider())
        await capabilities.create(ready_capability("filesystem.read"))
        await bindings.create(ready_binding())
        await agents.create(agent(name="reviewer-agent", role_name="reviewer"))
        item = await ready_work_item(work_items, work_item(role_name="coding"))

        decision = await scheduler(
            work_item_repository=work_items,
            agent_repository=agents,
            role_repository=roles,
            provider_repository=providers,
            capability_repository=capabilities,
            binding_repository=bindings,
            publisher=publisher,
        ).schedule_work_item(item.metadata.id)

        blocked = await work_items.get(item.metadata.id)

        assert decision.scheduled is False
        assert decision.reason == SchedulingReason.NO_ELIGIBLE_AGENT
        assert decision.evaluations[0].reasons == (SchedulingReason.ROLE_INCOMPATIBLE,)
        assert blocked.status.phase == WorkItemPhase.BLOCKED
        assert blocked.status.conditions[0].type == "Scheduled"
        assert blocked.status.conditions[0].reason == SchedulingReason.NO_ELIGIBLE_AGENT
        assert publisher.events[0].event_type == "WorkItemSchedulingBlocked"

        work_items.close()
        agents.close()
        roles.close()
        providers.close()
        capabilities.close()
        bindings.close()

    asyncio.run(scenario())


def test_scheduler_enforces_capability_admission() -> None:
    async def scenario() -> None:
        work_items = SQLiteWorkItemRepository(":memory:")
        agents = SQLiteAgentRepository(":memory:")
        roles = SQLiteRoleRepository(":memory:")
        providers = SQLiteProviderRepository(":memory:")
        capabilities = SQLiteCapabilityRepository(":memory:")
        bindings = SQLiteCapabilityBindingRepository(":memory:")

        await roles.create(ready_role(required_capabilities=("filesystem.write",)))
        await providers.create(provider())
        await capabilities.create(
            ready_capability(
                "filesystem.write",
                side_effect_level=CapabilitySideEffectLevel.MUTATING,
            )
        )
        await bindings.create(ready_binding(grants=("filesystem.read",)))
        await agents.create(agent(name="coder"))
        item = await ready_work_item(
            work_items,
            work_item(requested_capabilities=("filesystem.write",)),
        )

        decision = await scheduler(
            work_item_repository=work_items,
            agent_repository=agents,
            role_repository=roles,
            provider_repository=providers,
            capability_repository=capabilities,
            binding_repository=bindings,
        ).schedule_work_item(item.metadata.id)

        blocked = await work_items.get(item.metadata.id)

        assert decision.scheduled is False
        assert decision.evaluations[0].reasons == (SchedulingReason.CAPABILITY_DENIED,)
        assert "filesystem.write is not granted by policy" in (
            decision.evaluations[0].messages
        )
        assert blocked.status.phase == WorkItemPhase.BLOCKED

        work_items.close()
        agents.close()
        roles.close()
        providers.close()
        capabilities.close()
        bindings.close()

    asyncio.run(scenario())


def test_scheduler_selects_by_name_for_equal_eligible_candidates() -> None:
    async def scenario() -> None:
        work_items = SQLiteWorkItemRepository(":memory:")
        agents = SQLiteAgentRepository(":memory:")
        roles = SQLiteRoleRepository(":memory:")
        providers = SQLiteProviderRepository(":memory:")
        capabilities = SQLiteCapabilityRepository(":memory:")
        bindings = SQLiteCapabilityBindingRepository(":memory:")

        await roles.create(ready_role())
        await providers.create(provider())
        await capabilities.create(ready_capability("filesystem.read"))
        await bindings.create(ready_binding())
        await agents.create(agent(name="coder-z", priority=100, max_assignments=2))
        await agents.create(agent(name="coder-a", priority=100, max_assignments=2))
        item = await ready_work_item(work_items, work_item())

        decision = await scheduler(
            work_item_repository=work_items,
            agent_repository=agents,
            role_repository=roles,
            provider_repository=providers,
            capability_repository=capabilities,
            binding_repository=bindings,
        ).schedule_work_item(item.metadata.id)

        assert decision.scheduled is True
        assert decision.assigned_agent_ref is not None
        assert decision.assigned_agent_ref.name == "coder-a"

        work_items.close()
        agents.close()
        roles.close()
        providers.close()
        capabilities.close()
        bindings.close()

    asyncio.run(scenario())
