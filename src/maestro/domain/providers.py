"""Provider resources and model-runtime contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from maestro.domain.exceptions import MaestroDomainError
from maestro.domain.modeling import ModelIdentifier
from maestro.domain.repositories import (
    ResourceRepository,
    apply_spec_update,
    apply_status_update,
)
from maestro.domain.resources import (
    BaseResource,
    MaestroModel,
    Metadata,
    ResourceName,
    Spec,
    Status,
    utc_now,
)


class ProviderPhase(StrEnum):
    """Provider health phases."""

    PENDING = "Pending"
    READY = "Ready"
    DEGRADED = "Degraded"
    UNAVAILABLE = "Unavailable"
    DISABLED = "Disabled"


class ProviderErrorCode(StrEnum):
    """Normalized Provider failure codes."""

    PROVIDER_UNAVAILABLE = "ProviderUnavailable"
    PROVIDER_TIMEOUT = "ProviderTimeout"
    MODEL_UNAVAILABLE = "ModelUnavailable"
    INVALID_REQUEST = "InvalidRequest"
    STRUCTURED_OUTPUT_ERROR = "StructuredOutputError"
    TOOL_LOOP_ERROR = "ToolLoopError"
    UNKNOWN = "UnknownProviderError"


class ProviderMessageRole(StrEnum):
    """Provider message roles."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ProviderAuthReference(MaestroModel):
    """Reference to Provider authentication material."""

    kind: Literal["Secret"] = "Secret"
    name: ResourceName


class ProviderDataPolicy(MaestroModel):
    """Declared data policy for routing decisions."""

    allow_source_code: bool = Field(default=False, alias="allowSourceCode")
    allow_secrets: bool = Field(default=False, alias="allowSecrets")
    allow_personal_data: bool = Field(default=False, alias="allowPersonalData")


class ProviderFeatureSet(MaestroModel):
    """Provider operation capabilities."""

    structured_output: bool = Field(default=False, alias="structuredOutput")
    tool_calling: bool = Field(default=False, alias="toolCalling")
    streaming: bool = False


class ProviderSpec(Spec):
    """Provider runtime configuration."""

    provider_type: ResourceName = Field(alias="type")
    endpoint: str = Field(min_length=1)
    auth_ref: ProviderAuthReference | None = Field(default=None, alias="authRef")
    allowed_models: tuple[ModelIdentifier, ...] = Field(
        default_factory=tuple,
        alias="allowedModels",
    )
    data_policy: ProviderDataPolicy = Field(
        default_factory=ProviderDataPolicy,
        alias="dataPolicy",
    )
    timeout_seconds: int = Field(default=120, ge=1, alias="timeoutSeconds")

    @field_validator("allowed_models")
    @classmethod
    def reject_duplicate_allowed_models(
        cls,
        value: tuple[ModelIdentifier, ...],
    ) -> tuple[ModelIdentifier, ...]:
        """Reject duplicate allowed model identifiers."""

        if len(set(value)) != len(value):
            raise ValueError("allowedModels must be unique")
        return value


class ProviderFailure(MaestroModel):
    """Normalized Provider failure details."""

    code: ProviderErrorCode
    message: str = ""
    retryable: bool = False


class ProviderStatus(Status):
    """Observed Provider health and model discovery state."""

    phase: ProviderPhase = ProviderPhase.PENDING
    capabilities: ProviderFeatureSet = Field(default_factory=ProviderFeatureSet)
    available_models: tuple[ModelIdentifier, ...] = Field(
        default_factory=tuple,
        alias="availableModels",
    )
    last_health_check_at: datetime | None = Field(
        default=None,
        alias="lastHealthCheckAt",
    )
    failure: ProviderFailure | None = None

    @field_validator("available_models")
    @classmethod
    def reject_duplicate_available_models(
        cls,
        value: tuple[ModelIdentifier, ...],
    ) -> tuple[ModelIdentifier, ...]:
        """Reject duplicate discovered model identifiers."""

        if len(set(value)) != len(value):
            raise ValueError("availableModels must be unique")
        return value


class Provider(BaseResource[ProviderSpec, ProviderStatus]):
    """Model runtime Provider resource."""

    kind: Literal["Provider"] = "Provider"

    @model_validator(mode="after")
    def validate_provider_metadata_and_models(self) -> Self:
        """Validate reusable Provider metadata and allowed model boundaries."""

        for owner_reference in self.metadata.owner_references:
            if owner_reference.controller:
                raise ValueError("Provider resources cannot have controller owners")

        if self.spec.allowed_models:
            unexpected_models = set(self.status.available_models) - set(
                self.spec.allowed_models
            )
            if unexpected_models:
                raise ValueError(
                    "status.availableModels must be allowed by spec.allowedModels: "
                    + ", ".join(sorted(unexpected_models))
                )

        return self

    @classmethod
    def new(
        cls,
        *,
        name: ResourceName,
        spec: ProviderSpec,
        created_by: str = "local-user",
        namespace: ResourceName = "default",
    ) -> Self:
        """Create a new Provider resource."""

        return cls(
            metadata=Metadata(
                name=name,
                namespace=namespace,
                createdBy=created_by,
            ),
            spec=spec,
            status=ProviderStatus(),
        )


class ProviderHealth(MaestroModel):
    """Provider health contract returned by runtime adapters."""

    phase: ProviderPhase
    capabilities: ProviderFeatureSet = Field(default_factory=ProviderFeatureSet)
    available_models: tuple[ModelIdentifier, ...] = Field(
        default_factory=tuple,
        alias="availableModels",
    )
    failure: ProviderFailure | None = None


class ProviderModelList(MaestroModel):
    """Model discovery result."""

    models: tuple[ModelIdentifier, ...] = Field(default_factory=tuple)

    @field_validator("models")
    @classmethod
    def reject_duplicate_models(
        cls,
        value: tuple[ModelIdentifier, ...],
    ) -> tuple[ModelIdentifier, ...]:
        """Reject duplicate model identifiers."""

        if len(set(value)) != len(value):
            raise ValueError("models must be unique")
        return value


class ProviderMessage(MaestroModel):
    """Model-agnostic provider message."""

    role: ProviderMessageRole
    content: str


class ProviderTokenUsage(MaestroModel):
    """Provider token usage accounting."""

    input_tokens: int = Field(default=0, ge=0, alias="inputTokens")
    output_tokens: int = Field(default=0, ge=0, alias="outputTokens")


class StructuredGenerationRequest(MaestroModel):
    """Request for a structured model output."""

    model: ModelIdentifier
    messages: tuple[ProviderMessage, ...] = Field(min_length=1)
    response_schema: dict[str, Any] = Field(
        default_factory=dict,
        alias="responseSchema",
    )
    timeout_seconds: int = Field(default=120, ge=1, alias="timeoutSeconds")


class StructuredGenerationResult(MaestroModel):
    """Structured generation result."""

    model: ModelIdentifier
    output: dict[str, Any]
    raw_text: str = Field(default="", alias="rawText")
    token_usage: ProviderTokenUsage = Field(
        default_factory=ProviderTokenUsage,
        alias="tokenUsage",
    )


class ProviderToolDefinition(MaestroModel):
    """Tool definition exposed through the provider tool-loop contract."""

    name: ResourceName
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")


class ToolLoopRequest(MaestroModel):
    """Request for provider-managed tool-loop execution."""

    model: ModelIdentifier
    messages: tuple[ProviderMessage, ...] = Field(min_length=1)
    tools: tuple[ProviderToolDefinition, ...] = Field(default_factory=tuple)
    max_tool_calls: int = Field(default=40, ge=0, alias="maxToolCalls")
    timeout_seconds: int = Field(default=120, ge=1, alias="timeoutSeconds")

    @field_validator("tools")
    @classmethod
    def reject_duplicate_tools(
        cls,
        value: tuple[ProviderToolDefinition, ...],
    ) -> tuple[ProviderToolDefinition, ...]:
        """Reject duplicate tool names."""

        tool_names = [tool.name for tool in value]
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("tools must be unique by name")
        return value


class ToolLoopResult(MaestroModel):
    """Provider tool-loop result."""

    model: ModelIdentifier
    output: dict[str, Any]
    tool_call_count: int = Field(default=0, ge=0, alias="toolCallCount")
    token_usage: ProviderTokenUsage = Field(
        default_factory=ProviderTokenUsage,
        alias="tokenUsage",
    )


class ProviderOperationError(MaestroDomainError):
    """Raised by Provider adapters with normalized failure details."""

    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code}: {failure.message}")


class ModelProvider(Protocol):
    """Model-provider runtime contract."""

    async def health(self) -> ProviderHealth:
        """Return Provider health and discovered features."""

    async def list_models(self) -> ProviderModelList:
        """Return models discoverable from this Provider."""

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
    ) -> StructuredGenerationResult:
        """Generate a structured response."""

    async def run_tool_loop(self, request: ToolLoopRequest) -> ToolLoopResult:
        """Run a provider-managed tool loop."""


class ProviderRepository(
    ResourceRepository[Provider, ProviderSpec, ProviderStatus],
    Protocol,
):
    """Persistence contract for Provider resources."""

    async def get_by_name(self, namespace: str, name: str) -> Provider:
        """Load a Provider by namespace and name."""


def normalize_provider_error(error: Exception) -> ProviderFailure:
    """Normalize arbitrary Provider errors into a stable failure contract."""

    if isinstance(error, ProviderOperationError):
        return error.failure

    if isinstance(error, TimeoutError):
        return ProviderFailure(
            code=ProviderErrorCode.PROVIDER_TIMEOUT,
            message=str(error) or "Provider operation timed out",
            retryable=True,
        )

    return ProviderFailure(
        code=ProviderErrorCode.UNKNOWN,
        message=str(error) or type(error).__name__,
        retryable=False,
    )


def provider_status_from_health(
    provider: Provider,
    health: ProviderHealth,
    *,
    checked_at: datetime | None = None,
) -> ProviderStatus:
    """Build Provider status from a health response."""

    allowed_models = set(provider.spec.allowed_models)
    available_models = tuple(
        model
        for model in health.available_models
        if not allowed_models or model in allowed_models
    )
    return ProviderStatus(
        observedGeneration=provider.metadata.generation,
        phase=health.phase,
        capabilities=health.capabilities,
        availableModels=available_models,
        lastHealthCheckAt=checked_at or utc_now(),
        failure=health.failure,
    )


def apply_provider_spec_update(
    provider: Provider,
    spec: ProviderSpec,
    *,
    expected_resource_version: int,
) -> Provider:
    """Apply a Provider spec update."""

    return apply_spec_update(
        provider,
        spec,
        expected_resource_version=expected_resource_version,
    )


def apply_provider_status_update(
    provider: Provider,
    status: ProviderStatus,
    *,
    expected_resource_version: int,
) -> Provider:
    """Apply a Provider status update."""

    return apply_status_update(
        provider,
        status,
        expected_resource_version=expected_resource_version,
    )
