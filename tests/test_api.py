"""Tests for the FastAPI bootstrap application."""

import asyncio

from httpx import ASGITransport, AsyncClient, Response

from maestro.config import Settings
from maestro.presentation.api import create_app


async def _get(settings: Settings, path: str) -> Response:
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def test_liveness_endpoint_returns_ok(tmp_path) -> None:
    settings = Settings(
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
    )

    response = asyncio.run(_get(settings, "/health/live"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_endpoint_returns_ok(tmp_path) -> None:
    settings = Settings(
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
    )

    response = asyncio.run(_get(settings, "/health/ready"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
