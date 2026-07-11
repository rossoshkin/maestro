"""Tests for Maestro configuration."""

from maestro.config import Settings


def test_settings_use_documented_environment_prefix(monkeypatch) -> None:
    monkeypatch.setenv("MAESTRO_PORT", "8123")
    monkeypatch.setenv("MAESTRO_BIND_ADDRESS", "127.0.0.2")

    settings = Settings()

    assert settings.port == 8123
    assert settings.bind_address == "127.0.0.2"
