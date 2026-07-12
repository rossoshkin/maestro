"""Deterministic Agent scheduling for Ready WorkItems."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import cast
from uuid import UUID

from pydantic import Field

from maestro.application.controllers import observe_generation, with_condition
from maestro.domain.agents import (
    Agent,
    AgentPhase,
    AgentReadinessReason,
    AgentRepository,
    ProviderReadinessPhase,
    ProviderReadinessSnapshot,
    evaluate_agent_readiness,
    evaluate_agent_role_compatibility,
)
from maestro.domain.capabilities import (
    Capability,
    CapabilityBinding,
    CapabilityBindingRepository,
    CapabilityRepository,
    CapabilityResolutionContext,
    CapabilityRole,
    resolve_capabilities,
)
from maestro.domain.events import (
    EventDraft,
    EventExecutionReference,
    EventPayload,
    EventPublisher,
)
from maestro.domain.exceptions import ResourceNameNotFoundError
from maestro.domain.providers import ProviderRepository
from maestro.domain.repositories import ResourceSelector
from maestro.domain.resources import (
    ConditionStatus,
    MaestroModel,
    ResourceReference,
)
from maestro.domain.roles import Role, RoleRepository
from maestro.domain.work_items import (
    WorkItem,
    WorkItemAgentReference,
    WorkItemPhase,
    WorkItemRepository,
)


class SchedulingReason(StrEnum):
    """Structured scheduler outcome and rejection reasons."""

    ASSIGNED = "Assigned"
    WORK_ITEM_NOT_READY = "WorkItemNotReady"
    ROLE_NOT_FOUND = "RoleNotFound"
    ROLE_NOT_READY = "RoleNotReady"
    ROLE_INCOMPATIBLE = "RoleIncompatible"
    PROVIDER_NOT_FOUND = "ProviderNotFound"
    PROVIDER_NOT_READY = "ProviderNotReady"
    MODEL_UNAVAILABLE = "ModelUnavailable"
    AGENT_DISABLED = "AgentDisabled"
    AGENT_AT_CAPACITY = "AgentAtCapacity"
    CAPABILITY_DENIED = "CapabilityDenied"
    NO_ELIGIBLE_AGENT = "NoEligibleAgent"


class AgentSchedulingEvaluation(MaestroModel):
    """Auditable eligibility evaluation for one Agent."""

    agent_ref: WorkItemAgentReference = Field(alias="agentRef")
    eligible: bool
    reasons: tuple[SchedulingReason, ...]
    messages: tuple[str, ...] = Field(default_factory=tuple)
    priority: int
    current_assignments: int = Field(alias="currentAssignments")
    max_assignments: int = Field(alias="maxAssignments")
    granted_capabilities: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="grantedCapabilities",
    )
    effective_capabilities: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="effectiveCapabilities",
    )


class SchedulingDecision(MaestroModel):
    """Structured scheduler decision for one WorkItem."""

    scheduled: bool
    reason: SchedulingReason
    work_item_ref: ResourceReference = Field(alias="workItemRef")
    assigned_agent_ref: WorkItemAgentReference | None = Field(
        default=None,
        alias="assignedAgentRef",
    )
    evaluations: tuple[AgentSchedulingEvaluation, ...] = Field(default_factory=tuple)
    message: str = ""


class WorkItemScheduler:
    """Assign Ready WorkItems to eligible local Agents deterministically."""

    producer = "work-item-scheduler"

    def __init__(
        self,
        *,
        work_item_repository: WorkItemRepository,
        agent_repository: AgentRepository,
        role_repository: RoleRepository,
        provider_repository: ProviderRepository,
        capability_repository: CapabilityRepository,
        capability_binding_repository: CapabilityBindingRepository,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._work_item_repository = work_item_repository
        self._agent_repository = agent_repository
        self._role_repository = role_repository
        self._provider_repository = provider_repository
        self._capability_repository = capability_repository
        self._capability_binding_repository = capability_binding_repository
        self._event_publisher = event_publisher

    async def schedule_work_item(
        self,
        work_item_id: UUID,
        *,
        workspace_labels: dict[str, str] | None = None,
    ) -> SchedulingDecision:
        """Schedule a Ready WorkItem or record a structured blocked state."""

        work_item = await self._work_item_repository.get(work_item_id)
        if work_item.status.phase != WorkItemPhase.READY:
            decision = SchedulingDecision(
                scheduled=False,
                reason=SchedulingReason.WORK_ITEM_NOT_READY,
                workItemRef=_resource_ref(work_item),
                message=f"WorkItem is {work_item.status.phase}, not Ready",
            )
            await self._publish_decision(work_item, decision)
            return decision

        try:
            role = await self._role_repository.get_by_name_version(
                work_item.metadata.namespace,
                work_item.spec.role_ref.name,
                work_item.spec.role_ref.version,
            )
        except ResourceNameNotFoundError:
            decision = SchedulingDecision(
                scheduled=False,
                reason=SchedulingReason.ROLE_NOT_FOUND,
                workItemRef=_resource_ref(work_item),
                message=(
                    "Role "
                    f"{work_item.spec.role_ref.name}/"
                    f"{work_item.spec.role_ref.version} was not found"
                ),
            )
            blocked = await self._mark_blocked(work_item, decision)
            await self._publish_decision(blocked, decision)
            return decision

        evaluations = await self._evaluate_agents(
            work_item,
            role,
            workspace_labels=workspace_labels or {},
        )
        eligible = tuple(
            evaluation for evaluation in evaluations if evaluation.eligible
        )
        if not eligible:
            decision = SchedulingDecision(
                scheduled=False,
                reason=SchedulingReason.NO_ELIGIBLE_AGENT,
                workItemRef=_resource_ref(work_item),
                evaluations=evaluations,
                message="No eligible Agent can accept this WorkItem",
            )
            blocked = await self._mark_blocked(work_item, decision)
            await self._publish_decision(blocked, decision)
            return decision

        selected_evaluation = eligible[0]
        agent = await self._agent_repository.get(selected_evaluation.agent_ref.id)
        if (
            agent.status.current_assignments
            >= agent.spec.capacity.max_concurrent_assignments
        ):
            decision = SchedulingDecision(
                scheduled=False,
                reason=SchedulingReason.NO_ELIGIBLE_AGENT,
                workItemRef=_resource_ref(work_item),
                evaluations=evaluations,
                message="Selected Agent reached capacity before assignment",
            )
            blocked = await self._mark_blocked(work_item, decision)
            await self._publish_decision(blocked, decision)
            return decision

        updated_agent = await self._increment_agent_assignment(agent)
        try:
            scheduled_work_item = await self._assign_work_item(
                work_item,
                updated_agent,
            )
        except Exception:
            await self._decrement_agent_assignment(updated_agent)
            raise

        assigned_ref = _agent_ref(updated_agent)
        decision = SchedulingDecision(
            scheduled=True,
            reason=SchedulingReason.ASSIGNED,
            workItemRef=_resource_ref(scheduled_work_item),
            assignedAgentRef=assigned_ref,
            evaluations=evaluations,
            message=f"Assigned to Agent {updated_agent.metadata.name}",
        )
        await self._publish_decision(scheduled_work_item, decision)
        return decision

    async def _evaluate_agents(
        self,
        work_item: WorkItem,
        role: Role,
        *,
        workspace_labels: dict[str, str],
    ) -> tuple[AgentSchedulingEvaluation, ...]:
        agents = await self._agent_repository.list(
            ResourceSelector(namespace=work_item.metadata.namespace)
        )
        capabilities = await self._capability_repository.list(
            ResourceSelector(namespace=work_item.metadata.namespace)
        )
        ready_bindings = await self._capability_binding_repository.list_ready(
            work_item.metadata.namespace
        )

        evaluations: list[AgentSchedulingEvaluation] = []
        for agent in agents:
            evaluations.append(
                await self._evaluate_agent(
                    agent,
                    role,
                    work_item,
                    capabilities=capabilities,
                    ready_bindings=ready_bindings,
                    workspace_labels=workspace_labels,
                )
            )
        return tuple(sorted(evaluations, key=_evaluation_sort_key))

    async def _evaluate_agent(
        self,
        agent: Agent,
        role: Role,
        work_item: WorkItem,
        *,
        capabilities: tuple[Capability, ...],
        ready_bindings: tuple[CapabilityBinding, ...],
        workspace_labels: dict[str, str],
    ) -> AgentSchedulingEvaluation:
        reasons: list[SchedulingReason] = []
        messages: list[str] = []
        granted: tuple[str, ...] = ()
        effective: tuple[str, ...] = ()

        compatibility = evaluate_agent_role_compatibility(agent, role)
        if not compatibility.compatible:
            reason = (
                SchedulingReason.ROLE_NOT_READY
                if compatibility.reason.value == SchedulingReason.ROLE_NOT_READY
                else SchedulingReason.ROLE_INCOMPATIBLE
            )
            reasons.append(reason)
            messages.append(compatibility.message or str(compatibility.reason))

        try:
            provider = await self._provider_repository.get_by_name(
                agent.metadata.namespace,
                agent.spec.provider_ref.name,
            )
        except ResourceNameNotFoundError:
            reasons.append(SchedulingReason.PROVIDER_NOT_FOUND)
            messages.append(f"Provider {agent.spec.provider_ref.name} was not found")
        else:
            readiness = evaluate_agent_readiness(
                agent,
                ProviderReadinessSnapshot(
                    providerRef=agent.spec.provider_ref,
                    phase=ProviderReadinessPhase(provider.status.phase),
                    availableModels=provider.status.available_models,
                ),
            )
            if not readiness.can_accept_assignment:
                reasons.append(_readiness_reason(readiness.reason))
                messages.append(readiness.message or str(readiness.reason))

        agent_bindings = _bindings_for_agent(agent, ready_bindings)
        resolution = resolve_capabilities(
            role=cast(CapabilityRole, role),
            capabilities=capabilities,
            bindings=agent_bindings,
            requested_capabilities=work_item.spec.requested_capabilities,
            agent_supported_capabilities=_granted_capabilities(agent_bindings),
            context=CapabilityResolutionContext(workspaceLabels=workspace_labels),
        )
        granted = resolution.granted
        effective = resolution.effective
        if not resolution.allowed:
            reasons.append(SchedulingReason.CAPABILITY_DENIED)
            messages.extend(violation.message for violation in resolution.violations)

        return AgentSchedulingEvaluation(
            agentRef=_agent_ref(agent),
            eligible=not reasons,
            reasons=tuple(reasons),
            messages=tuple(message for message in messages if message),
            priority=agent.spec.scheduling.priority,
            currentAssignments=agent.status.current_assignments,
            maxAssignments=agent.spec.capacity.max_concurrent_assignments,
            grantedCapabilities=granted,
            effectiveCapabilities=effective,
        )

    async def _increment_agent_assignment(self, agent: Agent) -> Agent:
        current_assignments = agent.status.current_assignments + 1
        phase = (
            AgentPhase.BUSY
            if current_assignments >= agent.spec.capacity.max_concurrent_assignments
            else AgentPhase.READY
        )
        status = observe_generation(
            agent,
            agent.status.model_copy(
                update={
                    "phase": phase,
                    "current_assignments": current_assignments,
                }
            ),
        )
        return await self._agent_repository.update_status(
            agent.metadata.id,
            status,
            expected_resource_version=agent.metadata.resource_version,
        )

    async def _decrement_agent_assignment(self, agent: Agent) -> Agent:
        current = await self._agent_repository.get(agent.metadata.id)
        current_assignments = max(0, current.status.current_assignments - 1)
        phase = (
            AgentPhase.READY
            if current.status.phase == AgentPhase.BUSY
            and current_assignments < current.spec.capacity.max_concurrent_assignments
            else current.status.phase
        )
        status = observe_generation(
            current,
            current.status.model_copy(
                update={
                    "phase": phase,
                    "current_assignments": current_assignments,
                }
            ),
        )
        return await self._agent_repository.update_status(
            current.metadata.id,
            status,
            expected_resource_version=current.metadata.resource_version,
        )

    async def _assign_work_item(self, work_item: WorkItem, agent: Agent) -> WorkItem:
        status = work_item.status.model_copy(
            update={
                "phase": WorkItemPhase.SCHEDULED,
                "assigned_agent_ref": _agent_ref(agent),
            }
        )
        status = with_condition(
            work_item,
            observe_generation(work_item, status),
            condition_type="Scheduled",
            condition_status=ConditionStatus.TRUE,
            reason=SchedulingReason.ASSIGNED,
            message=f"Assigned to Agent {agent.metadata.name}",
        )
        return await self._work_item_repository.update_status(
            work_item.metadata.id,
            status,
            expected_resource_version=work_item.metadata.resource_version,
        )

    async def _mark_blocked(
        self,
        work_item: WorkItem,
        decision: SchedulingDecision,
    ) -> WorkItem:
        if work_item.status.phase != WorkItemPhase.READY:
            return work_item

        status = work_item.status.model_copy(update={"phase": WorkItemPhase.BLOCKED})
        status = with_condition(
            work_item,
            observe_generation(work_item, status),
            condition_type="Scheduled",
            condition_status=ConditionStatus.FALSE,
            reason=decision.reason,
            message=decision.message,
        )
        return await self._work_item_repository.update_status(
            work_item.metadata.id,
            status,
            expected_resource_version=work_item.metadata.resource_version,
        )

    async def _publish_decision(
        self,
        work_item: WorkItem,
        decision: SchedulingDecision,
    ) -> None:
        if self._event_publisher is None:
            return
        event_type = (
            "WorkItemScheduled" if decision.scheduled else "WorkItemSchedulingBlocked"
        )
        await self._event_publisher.publish(
            EventDraft(
                type=event_type,
                occurredAt=work_item.metadata.updated_at,
                producer=self.producer,
                correlationId=(
                    "schedule:"
                    f"{work_item.metadata.id}:"
                    f"{work_item.metadata.resource_version}"
                ),
                executionRef=EventExecutionReference(
                    id=work_item.spec.execution_ref.id,
                    name=work_item.spec.execution_ref.name,
                ),
                subjectRef=_resource_ref(work_item),
                payload=_decision_payload(decision),
            )
        )


def _readiness_reason(reason: AgentReadinessReason) -> SchedulingReason:
    match reason:
        case AgentReadinessReason.DISABLED:
            return SchedulingReason.AGENT_DISABLED
        case AgentReadinessReason.BUSY:
            return SchedulingReason.AGENT_AT_CAPACITY
        case AgentReadinessReason.MODEL_UNAVAILABLE:
            return SchedulingReason.MODEL_UNAVAILABLE
        case _:
            return SchedulingReason.PROVIDER_NOT_READY


def _bindings_for_agent(
    agent: Agent,
    ready_bindings: tuple[CapabilityBinding, ...],
) -> tuple[CapabilityBinding, ...]:
    if not agent.spec.capability_bindings:
        return ()
    ready_by_name = {binding.metadata.name: binding for binding in ready_bindings}
    return tuple(
        ready_by_name[reference.name]
        for reference in agent.spec.capability_bindings
        if reference.name in ready_by_name
    )


def _granted_capabilities(
    bindings: Iterable[CapabilityBinding],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {capability for binding in bindings for capability in binding.spec.grants}
        )
    )


def _evaluation_sort_key(
    evaluation: AgentSchedulingEvaluation,
) -> tuple[int, int, int, str]:
    return (
        0 if evaluation.eligible else 1,
        -evaluation.priority,
        evaluation.current_assignments,
        evaluation.agent_ref.name or "",
    )


def _agent_ref(agent: Agent) -> WorkItemAgentReference:
    return WorkItemAgentReference(
        id=agent.metadata.id,
        name=agent.metadata.name,
    )


def _resource_ref(resource: WorkItem) -> ResourceReference:
    return ResourceReference(
        kind=resource.kind,
        id=resource.metadata.id,
        name=resource.metadata.name,
    )


def _decision_payload(decision: SchedulingDecision) -> EventPayload:
    payload = decision.model_dump(mode="json", by_alias=True)
    return {
        "scheduled": payload["scheduled"],
        "reason": payload["reason"],
        "message": payload["message"],
        "assignedAgentRef": payload["assignedAgentRef"],
        "evaluations": payload["evaluations"],
    }
