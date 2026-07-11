"""FastAPI application for the Maestro control plane."""

from fastapi import FastAPI
from pydantic import BaseModel

from maestro import __version__
from maestro.config import Settings, get_settings
from maestro.logging import configure_logging


class HealthResponse(BaseModel):
    """Health endpoint response."""

    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the Maestro FastAPI application."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    api = FastAPI(
        title="Maestro",
        description="Local-first AI orchestration control plane.",
        version=__version__,
    )

    @api.get("/health/live", response_model=HealthResponse, tags=["health"])
    def liveness() -> HealthResponse:
        """Report that the API process is alive."""

        return HealthResponse(status="ok")

    @api.get("/health/ready", response_model=HealthResponse, tags=["health"])
    def readiness() -> HealthResponse:
        """Report that the API process is ready for bootstrap traffic."""

        return HealthResponse(status="ok")

    return api


app = create_app()
