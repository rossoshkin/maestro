"""Typer command line interface for Maestro."""

from typing import Annotated

import typer

from maestro import __version__
from maestro.config import Settings, get_settings
from maestro.logging import configure_logging

app = typer.Typer(
    help="Maestro local-first AI orchestration control plane.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"maestro {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            help="Show the Maestro version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Run Maestro command line actions."""


@app.command()
def serve(
    host: Annotated[
        str | None,
        typer.Option("--host", help="Bind address for the API server."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", min=1, max=65535, help="Port for the API server."),
    ] = None,
) -> None:
    """Start the local Maestro API server."""

    import uvicorn

    settings = get_settings()
    effective_settings = Settings(
        database_url=settings.database_url,
        artifact_root=settings.artifact_root,
        workspace_root=settings.workspace_root,
        log_level=settings.log_level,
        bind_address=host or settings.bind_address,
        port=port or settings.port,
    )
    configure_logging(effective_settings)
    uvicorn.run(
        "maestro.presentation.api:app",
        host=effective_settings.bind_address,
        port=effective_settings.port,
        log_level=effective_settings.log_level.lower(),
    )
