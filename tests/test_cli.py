"""Tests for the Typer bootstrap CLI."""

from typer.testing import CliRunner

from maestro.presentation.cli import app


def test_cli_help_renders() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Maestro local-first AI orchestration control plane" in result.output


def test_cli_version_renders() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("maestro ")
