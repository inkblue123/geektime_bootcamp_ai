"""Unit tests for multi-database and security configuration."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pg_mcp.config.settings import (
    DatabaseConfig,
    DatabaseSecurityConfig,
    OpenAIConfig,
    SecurityConfig,
    Settings,
)


def make_settings(**kwargs: object) -> Settings:
    """Build settings without depending on the ambient environment."""
    kwargs.setdefault("openai", OpenAIConfig(api_key="sk-test-offline"))
    return Settings(**kwargs)  # type: ignore[arg-type]


class TestMultiDatabaseSettings:
    """`Settings.databases` and the resolution helpers."""

    def test_single_database_default(self) -> None:
        settings = make_settings()
        assert settings.databases == []
        assert settings.database_names() == ["postgres"]
        assert len(settings.resolved_databases()) == 1

    def test_additional_databases_are_resolved(self) -> None:
        settings = make_settings(
            databases=[
                DatabaseConfig(name="analytics", host="analytics.internal", port=5433),
                DatabaseConfig(name="sales", host="sales.internal"),
            ]
        )
        assert settings.database_names() == ["postgres", "analytics", "sales"]
        assert settings.get_database("sales") is not None
        assert settings.get_database("sales").host == "sales.internal"
        assert settings.get_database("missing") is None

    def test_name_collision_with_primary_is_rejected(self) -> None:
        """The primary database name may not be repeated in `databases`."""
        with pytest.raises(ValidationError, match="Duplicate database names"):
            make_settings(
                database=DatabaseConfig(name="primary", host="primary.internal"),
                databases=[DatabaseConfig(name="primary", host="ignored.internal")],
            )

    def test_duplicate_names_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate database names"):
            make_settings(
                database=DatabaseConfig(name="dup"),
                databases=[DatabaseConfig(name="other"), DatabaseConfig(name="other")],
            )

    def test_databases_can_be_loaded_from_json_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "DATABASES",
            json.dumps(
                [
                    {"name": "analytics", "host": "a.internal", "port": 5433},
                    {"name": "sales", "host": "s.internal"},
                ]
            ),
        )
        monkeypatch.setenv("DATABASE_NAME", "primary")

        settings = make_settings()

        assert settings.database_names() == ["primary", "analytics", "sales"]
        assert settings.get_database("analytics").port == 5433

    def test_blank_database_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            DatabaseConfig(name="   ")


class TestSecurityConfig:
    """Security policy fields and per-database overrides."""

    def test_defaults_deny_by_default(self) -> None:
        config = SecurityConfig()
        assert config.blocked_tables == []
        assert config.blocked_columns == []
        assert config.allow_explain is False
        assert config.allow_write_operations is False
        assert "pg_sleep" in config.blocked_functions

    def test_blocked_lists_accept_comma_separated_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECURITY_BLOCKED_TABLES", "secret_data, payment_cards")
        monkeypatch.setenv("SECURITY_BLOCKED_COLUMNS", "password_hash")

        config = SecurityConfig()

        assert config.blocked_tables == ["secret_data", "payment_cards"]
        assert config.blocked_columns == ["password_hash"]

    def test_effective_for_without_override_returns_global(self) -> None:
        config = SecurityConfig(blocked_tables=["secrets"])
        assert config.effective_for("anydb") is config

    def test_per_database_override_is_additive(self) -> None:
        config = SecurityConfig(
            blocked_tables=["secrets"],
            blocked_columns=["password_hash"],
            per_database={
                "analytics": DatabaseSecurityConfig(
                    blocked_tables=["analytics_pii"],
                    blocked_columns=["ssn"],
                    max_rows=100,
                    readonly_role="analytics_ro",
                )
            },
        )

        effective = config.effective_for("analytics")

        assert effective.blocked_tables == ["secrets", "analytics_pii"]
        assert effective.blocked_columns == ["password_hash", "ssn"]
        assert effective.max_rows == 100
        assert effective.readonly_role == "analytics_ro"
        # The global config must not be mutated
        assert config.blocked_tables == ["secrets"]
        assert config.max_rows == 10000

    def test_per_database_override_can_enable_explain(self) -> None:
        config = SecurityConfig(
            allow_explain=False,
            per_database={"reporting": DatabaseSecurityConfig(allow_explain=True)},
        )

        assert config.effective_for("reporting").allow_explain is True
        assert config.effective_for("other").allow_explain is False

    def test_per_database_cannot_grant_blocked_table(self) -> None:
        """Overrides are additive: they can only restrict further."""
        config = SecurityConfig(
            blocked_tables=["secrets"],
            per_database={"reporting": DatabaseSecurityConfig(blocked_tables=[])},
        )
        assert config.effective_for("reporting").blocked_tables == ["secrets"]

    def test_per_database_accepts_json_string(self) -> None:
        config = SecurityConfig(per_database=json.dumps({"analytics": {"blocked_tables": ["pii"]}}))
        assert config.effective_for("analytics").blocked_tables == ["pii"]


class TestResilienceConfig:
    """Resilience fields are all wired to real behaviour."""

    def test_rate_limit_defaults(self) -> None:
        from pg_mcp.config.settings import ResilienceConfig

        config = ResilienceConfig()
        assert config.rate_limit_enabled is True
        assert config.max_concurrent_queries == 10
        assert config.max_concurrent_llm_calls == 5
        assert config.max_retry_delay == 30.0
        assert config.retry_jitter == 0.1

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_concurrent_queries": 0},
            {"max_concurrent_llm_calls": 0},
            {"rate_limit_timeout": 0.0},
            {"retry_jitter": 2.0},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs: dict) -> None:
        from pg_mcp.config.settings import ResilienceConfig

        with pytest.raises(ValidationError):
            ResilienceConfig(**kwargs)
