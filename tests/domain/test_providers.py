"""Tests for Provider resources and runtime contracts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from maestro.domain.providers import (
    Provider,
    ProviderAuthReference,
    ProviderDataPolicy,
    ProviderErrorCode,
    ProviderFailure,
    ProviderFeatureSet,
    ProviderHealth,
    ProviderMessage,
    ProviderMessageRole,
    ProviderOperationError,
    ProviderPhase,
    ProviderSpec,
    ProviderStatus,
    ProviderToolDefinition,
    StructuredGenerationRequest,
    ToolLoopRequest,
    normalize_provider_error,
    provider_status_from_health,
)
from maestro.domain.resources import Metadata, OwnerReference


def valid_provider_spec() -> ProviderSpec:
    """Build a valid ProviderSpec for tests."""

    return ProviderSpec(
        type="ollama",
        endpoint="http://127.0.0.1:11434",
        authRef=None,
        allowedModels=("qwen3:14b", "qwen2.5-coder:14b"),
        dataPolicy=ProviderDataPolicy(
            allowSourceCode=True,
            allowSecrets=False,
            allowPersonalData=False,
        ),
        timeoutSeconds=120,
    )


def valid_provider() -> Provider:
    """Build a valid Provider resource."""

    return Provider.new(name="ollama-local", spec=valid_provider_spec())


def test_provider_serializes_and_deserializes() -> None:
    provider = valid_provider()

    payload = provider.model_dump(mode="json", by_alias=True)
    round_tripped = Provider.model_validate(payload)

    assert payload["kind"] == "Provider"
    assert payload["spec"]["type"] == "ollama"
    assert payload["spec"]["allowedModels"] == ["qwen3:14b", "qwen2.5-coder:14b"]
    assert round_tripped == provider


def test_provider_rejects_controller_owner_references() -> None:
    with pytest.raises(ValidationError):
        Provider(
            metadata=Metadata(
                name="ollama-local",
                ownerReferences=(
                    OwnerReference(kind="Project", id=uuid4(), controller=True),
                ),
            ),
            spec=valid_provider_spec(),
            status=ProviderStatus(),
        )


def test_credentials_are_referenced_not_embedded() -> None:
    payload = valid_provider_spec().model_dump(mode="json", by_alias=True)
    payload["apiKey"] = "secret-value"

    with pytest.raises(ValidationError):
        ProviderSpec.model_validate(payload)


def test_auth_reference_is_structural() -> None:
    spec = valid_provider_spec().model_copy(
        update={"auth_ref": ProviderAuthReference(name="ollama-token")}
    )

    assert spec.auth_ref is not None
    assert spec.auth_ref.name == "ollama-token"


def test_duplicate_allowed_models_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderSpec(
            type="ollama",
            endpoint="http://127.0.0.1:11434",
            allowedModels=("qwen3:14b", "qwen3:14b"),
        )


def test_available_models_must_be_allowed_when_allow_list_exists() -> None:
    provider = valid_provider()

    with pytest.raises(ValidationError):
        Provider(
            metadata=provider.metadata,
            spec=provider.spec,
            status=ProviderStatus(
                phase=ProviderPhase.READY,
                availableModels=("not-allowed:latest",),
            ),
        )


def test_provider_status_from_health_filters_to_allowed_models() -> None:
    provider = valid_provider()
    health = ProviderHealth(
        phase=ProviderPhase.READY,
        capabilities=ProviderFeatureSet(structuredOutput=True),
        availableModels=("qwen3:14b", "not-allowed:latest"),
    )

    status = provider_status_from_health(provider, health)

    assert status.phase == ProviderPhase.READY
    assert status.available_models == ("qwen3:14b",)


def test_timeout_seconds_are_bounded() -> None:
    with pytest.raises(ValidationError):
        ProviderSpec(type="ollama", endpoint="http://127.0.0.1:11434", timeoutSeconds=0)


def test_structured_generation_request_requires_messages() -> None:
    with pytest.raises(ValidationError):
        StructuredGenerationRequest(model="qwen3:14b", messages=())


def test_tool_loop_rejects_duplicate_tool_names() -> None:
    message = ProviderMessage(role=ProviderMessageRole.USER, content="Run tests")

    with pytest.raises(ValidationError):
        ToolLoopRequest(
            model="qwen3:14b",
            messages=(message,),
            tools=(
                ProviderToolDefinition(name="pytest"),
                ProviderToolDefinition(name="pytest"),
            ),
        )


def test_provider_operation_error_normalizes_failure() -> None:
    failure = ProviderFailure(
        code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
        message="Provider is down",
        retryable=True,
    )

    normalized = normalize_provider_error(ProviderOperationError(failure))

    assert normalized == failure


def test_timeout_error_normalizes_to_provider_timeout() -> None:
    normalized = normalize_provider_error(TimeoutError("slow"))

    assert normalized.code == ProviderErrorCode.PROVIDER_TIMEOUT
    assert normalized.retryable is True
