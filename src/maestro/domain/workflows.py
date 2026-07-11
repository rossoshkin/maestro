"""Workflow resource models and graph validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import ResourceImmutableFieldError
from maestro.domain.projects import ReferenceVersion
from maestro.domain.repositories import ResourceRepository, apply_spec_update
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    ResourceName,
    Spec,
    Status,
)

StepId = ResourceName
Expression = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.\-\[\]]+$"),
]


class WorkflowPhase(StrEnum):
    """Workflow status phases."""

    PENDING = "Pending"
    VALIDATING = "Validating"
    READY = "Ready"
    INVALID = "Invalid"
    DEPRECATED = "Deprecated"


class WorkflowStepType(StrEnum):
    """Supported Workflow step types."""

    ROLE = "role"
    SYSTEM = "system"
    APPROVAL = "approval"
    FANOUT = "fanout"
    JOIN = "join"
    DECISION = "decision"
    TERMINAL = "terminal"


class TerminalOutcome(StrEnum):
    """Terminal Workflow outcomes."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class JoinSuccessPolicy(StrEnum):
    """Join step success policy."""

    ALL = "all"


class WorkflowValidationResult(MaestroModel):
    """Workflow validation result stored in status."""

    valid: bool = False
    errors: tuple[str, ...] = Field(default_factory=tuple)


class WorkflowRetryPolicy(MaestroModel):
    """Finite retry policy for a Workflow step."""

    max_attempts: int = Field(ge=1, alias="maxAttempts")
    retry_on: tuple[str, ...] = Field(default_factory=tuple, alias="retryOn")
    do_not_retry_on: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="doNotRetryOn",
    )


class WorkflowRoleReference(MaestroModel):
    """Role version referenced by a Workflow step."""

    name: ResourceName
    version: ReferenceVersion


class WorkflowStepBase(MaestroModel):
    """Shared fields for non-terminal Workflow steps."""

    id: StepId
    retry_policy: WorkflowRetryPolicy | None = Field(
        default=None,
        alias="retryPolicy",
    )
    on_success: StepId | None = Field(default=None, alias="onSuccess")
    on_failure: StepId | None = Field(default=None, alias="onFailure")

    def transition_targets(self) -> tuple[StepId, ...]:
        """Return outgoing transition targets declared by this step."""

        return tuple(
            target
            for target in (self.on_success, self.on_failure)
            if target is not None
        )

    def has_finite_retry_bound(self) -> bool:
        """Return whether this step bounds retries."""

        return self.retry_policy is not None


class WorkflowRoleStep(WorkflowStepBase):
    """Workflow step that schedules a Role invocation."""

    type: Literal[WorkflowStepType.ROLE] = WorkflowStepType.ROLE
    role_ref: WorkflowRoleReference = Field(alias="roleRef")
    max_attempts: int | None = Field(default=None, ge=1, alias="maxAttempts")
    on_approved: StepId | None = Field(default=None, alias="onApproved")
    on_changes_requested: StepId | None = Field(
        default=None,
        alias="onChangesRequested",
    )
    on_needs_human_decision: StepId | None = Field(
        default=None,
        alias="onNeedsHumanDecision",
    )

    def transition_targets(self) -> tuple[StepId, ...]:
        """Return outgoing transition targets declared by this step."""

        return (
            *super().transition_targets(),
            *tuple(
                target
                for target in (
                    self.on_approved,
                    self.on_changes_requested,
                    self.on_needs_human_decision,
                )
                if target is not None
            ),
        )

    def has_finite_retry_bound(self) -> bool:
        """Return whether this step bounds retries."""

        return self.max_attempts is not None or super().has_finite_retry_bound()


class WorkflowSystemStep(WorkflowStepBase):
    """Workflow step handled by a deterministic Maestro controller."""

    type: Literal[WorkflowStepType.SYSTEM] = WorkflowStepType.SYSTEM
    controller: ResourceName


class WorkflowApprovalStep(WorkflowStepBase):
    """Workflow step that waits for an approval decision."""

    type: Literal[WorkflowStepType.APPROVAL] = WorkflowStepType.APPROVAL
    subject: str | None = Field(default=None, min_length=1)
    subject_ref: str | None = Field(default=None, min_length=1, alias="subjectRef")
    required_approvers: int = Field(default=1, ge=1, alias="requiredApprovers")
    on_approved: StepId | None = Field(default=None, alias="onApproved")
    on_rejected: StepId | None = Field(default=None, alias="onRejected")

    @model_validator(mode="after")
    def require_subject(self) -> Self:
        """Require an approval subject."""

        if self.subject is None and self.subject_ref is None:
            raise ValueError("approval steps require subject or subjectRef")
        return self

    def transition_targets(self) -> tuple[StepId, ...]:
        """Return outgoing transition targets declared by this step."""

        return (
            *super().transition_targets(),
            *tuple(
                target
                for target in (self.on_approved, self.on_rejected)
                if target is not None
            ),
        )


class WorkflowFanOutStep(WorkflowStepBase):
    """Workflow step that fans out Work Items from a source list."""

    type: Literal[WorkflowStepType.FANOUT] = WorkflowStepType.FANOUT
    source: str = Field(min_length=1)
    role_field: str | None = Field(default=None, min_length=1, alias="roleField")
    max_parallel: int = Field(default=1, ge=1, alias="maxParallel")


class WorkflowJoinStep(WorkflowStepBase):
    """Workflow step that joins Work Item results."""

    type: Literal[WorkflowStepType.JOIN] = WorkflowStepType.JOIN
    source: str = Field(min_length=1)
    success_policy: JoinSuccessPolicy = Field(
        default=JoinSuccessPolicy.ALL,
        alias="successPolicy",
    )


class WorkflowDecisionStep(WorkflowStepBase):
    """Workflow step that routes using a deterministic expression."""

    type: Literal[WorkflowStepType.DECISION] = WorkflowStepType.DECISION
    expression: Expression
    cases: dict[str, StepId] = Field(default_factory=dict)
    default: StepId | None = None

    @model_validator(mode="after")
    def require_route(self) -> Self:
        """Require at least one decision route."""

        if not self.cases and self.default is None:
            raise ValueError("decision steps require cases or default")
        return self

    def transition_targets(self) -> tuple[StepId, ...]:
        """Return outgoing transition targets declared by this step."""

        return (
            *super().transition_targets(),
            *tuple(self.cases.values()),
            *(() if self.default is None else (self.default,)),
        )


class WorkflowTerminalStep(MaestroModel):
    """Workflow terminal step."""

    id: StepId
    type: Literal[WorkflowStepType.TERMINAL] = WorkflowStepType.TERMINAL
    outcome: TerminalOutcome

    def transition_targets(self) -> tuple[StepId, ...]:
        """Return outgoing transition targets declared by this step."""

        return ()

    def has_finite_retry_bound(self) -> bool:
        """Return whether this step bounds retries."""

        return False


WorkflowStep = Annotated[
    WorkflowRoleStep
    | WorkflowSystemStep
    | WorkflowApprovalStep
    | WorkflowFanOutStep
    | WorkflowJoinStep
    | WorkflowDecisionStep
    | WorkflowTerminalStep,
    Field(discriminator="type"),
]


class WorkflowSpec(Spec):
    """Declarative Workflow graph definition."""

    version: ReferenceVersion
    description: str = ""
    entrypoint: StepId
    parameters: dict[str, Any] = Field(default_factory=dict)
    steps: tuple[WorkflowStep, ...]
    policies: dict[str, Any] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def reject_duplicate_step_ids(
        cls,
        value: tuple[WorkflowStep, ...],
    ) -> tuple[WorkflowStep, ...]:
        """Reject duplicate step IDs."""

        step_ids = [step.id for step in value]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("Workflow step IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        """Validate Workflow graph structure."""

        validate_workflow_graph(self)
        return self


class WorkflowStatus(Status):
    """Observed state for a Workflow definition."""

    phase: WorkflowPhase = WorkflowPhase.PENDING
    validation: WorkflowValidationResult = Field(
        default_factory=WorkflowValidationResult
    )


class Workflow(BaseResource[WorkflowSpec, WorkflowStatus]):
    """Immutable, versioned Workflow definition."""

    kind: Literal["Workflow"] = "Workflow"

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: WorkflowSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Workflow resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=WorkflowStatus(),
        )


class WorkflowRepository(
    ResourceRepository[Workflow, WorkflowSpec, WorkflowStatus],
    Protocol,
):
    """Persistence contract for Workflow resources."""

    async def get_by_name_version(
        self,
        namespace: str,
        name: str,
        version: str,
    ) -> Workflow:
        """Load a Workflow by namespace, name and version."""


def validate_workflow_graph(spec: WorkflowSpec) -> None:
    """Validate step IDs, transition targets, reachability and bounded cycles."""

    step_by_id = {step.id: step for step in spec.steps}
    if spec.entrypoint not in step_by_id:
        raise ValueError("Workflow entrypoint must reference an existing step")

    terminal_step_ids = {
        step.id for step in spec.steps if isinstance(step, WorkflowTerminalStep)
    }
    if not terminal_step_ids:
        raise ValueError("Workflow must contain at least one terminal step")

    graph = {step.id: tuple(step.transition_targets()) for step in spec.steps}

    for source, targets in graph.items():
        for target in targets:
            if target not in step_by_id:
                raise ValueError(
                    f"Workflow step {source!r} references missing transition target "
                    f"{target!r}"
                )

    reachable = _reachable_from(spec.entrypoint, graph)
    unreachable_steps = set(step_by_id) - reachable
    if unreachable_steps:
        raise ValueError(
            "Workflow contains unreachable steps: "
            + ", ".join(sorted(unreachable_steps))
        )

    unreachable_terminals = terminal_step_ids - reachable
    if unreachable_terminals:
        raise ValueError(
            "Workflow contains unreachable terminal steps: "
            + ", ".join(sorted(unreachable_terminals))
        )

    can_reach_terminal = _nodes_that_can_reach_terminal(graph, terminal_step_ids)
    blocked_steps = reachable - can_reach_terminal
    if blocked_steps:
        raise ValueError(
            "Workflow steps cannot reach a terminal step: "
            + ", ".join(sorted(blocked_steps))
        )

    for component in _strongly_connected_components(graph):
        cyclic = len(component) > 1 or any(node in graph[node] for node in component)
        if cyclic and not any(
            step_by_id[node].has_finite_retry_bound() for node in component
        ):
            raise ValueError(
                "Workflow contains an unbounded cycle: " + ", ".join(sorted(component))
            )


def apply_workflow_spec_update(
    workflow: Workflow,
    spec: WorkflowSpec,
    *,
    expected_resource_version: int,
) -> Workflow:
    """Reject actual Workflow spec changes because versions are immutable."""

    if spec != workflow.spec:
        raise ResourceImmutableFieldError(workflow.metadata.id, "spec")

    return apply_spec_update(
        workflow,
        spec,
        expected_resource_version=expected_resource_version,
    )


def _reachable_from(
    start: StepId, graph: dict[StepId, tuple[StepId, ...]]
) -> set[StepId]:
    visited: set[StepId] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(graph[node])
    return visited


def _nodes_that_can_reach_terminal(
    graph: dict[StepId, tuple[StepId, ...]],
    terminal_step_ids: set[StepId],
) -> set[StepId]:
    reverse_graph: dict[StepId, set[StepId]] = {node: set() for node in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse_graph[target].add(source)

    reachable = set(terminal_step_ids)
    stack = list(terminal_step_ids)
    while stack:
        node = stack.pop()
        for predecessor in reverse_graph[node]:
            if predecessor not in reachable:
                reachable.add(predecessor)
                stack.append(predecessor)
    return reachable


def _strongly_connected_components(
    graph: dict[StepId, tuple[StepId, ...]],
) -> tuple[frozenset[StepId], ...]:
    index = 0
    stack: list[StepId] = []
    on_stack: set[StepId] = set()
    indexes: dict[StepId, int] = {}
    lowlinks: dict[StepId, int] = {}
    components: list[frozenset[StepId]] = []

    def visit(node: StepId) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in graph[node]:
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] == indexes[node]:
            component: set[StepId] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node:
                    break
            components.append(frozenset(component))

    for node in graph:
        if node not in indexes:
            visit(node)

    return tuple(components)
