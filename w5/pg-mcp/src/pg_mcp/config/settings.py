"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.

Multi-database support
----------------------
``Settings`` exposes two complementary fields:

* ``database`` (``DatabaseConfig``) - the primary/default database, configured
  through the classic ``DATABASE_*`` environment variables.
* ``databases`` (``list[DatabaseConfig]``) - optional additional databases,
  configured through the ``DATABASES`` environment variable as a JSON array.
  Each entry describes one database and may point at a different host/port.

When ``databases`` is empty the server runs in single-database mode and simply
uses ``database``. When it is populated, every entry (including ``database``)
becomes an addressable target for the ``database`` argument of the ``query``
MCP tool.

Example:
    >>> import os
    >>> os.environ["DATABASES"] = (
    ...     '[{"name": "analytics", "host": "analytics.internal"}]'
    ... )
    >>> settings = Settings()
    >>> [db.name for db in settings.resolved_databases()]
    ['postgres', 'analytics']
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: List fields that accept either a JSON array or a comma-separated string from
#: the environment. ``NoDecode`` stops pydantic-settings from trying to
#: ``json.loads`` the raw value so the validators below can accept both forms.
EnvList = Annotated[list[str], NoDecode]


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    name: str = Field(default="postgres", description="Database name")
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate database name is not empty."""
        if not v or not v.strip():
            raise ValueError("Database name must not be empty")
        return v.strip()


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(
        default=2000, ge=100, le=128000, description="Maximum tokens in response"
    )
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Optional OpenAI-compatible API base URL. Useful for proxies, gateway "
            "deployments and local test doubles."
        ),
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty and has correct format."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        if not api_key_str.startswith("sk-"):
            raise ValueError("OpenAI API key must start with 'sk-'")
        return v


class DatabaseSecurityConfig(BaseSettings):
    """Per-database security override.

    Every field is optional: ``None`` means "inherit from the global
    :class:`SecurityConfig`". Restrictions (blocked functions/tables/columns)
    are *additive* - a per-database override can only ever restrict further,
    never grant access that the global policy denies.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_DB_")

    blocked_functions: EnvList | None = Field(
        default=None, description="Additional blocked PostgreSQL functions"
    )
    blocked_tables: EnvList | None = Field(
        default=None, description="Additional blocked tables for this database"
    )
    blocked_columns: EnvList | None = Field(
        default=None, description="Additional blocked columns for this database"
    )
    allow_explain: bool | None = Field(
        default=None, description="Override the global EXPLAIN policy for this database"
    )
    max_rows: int | None = Field(
        default=None, ge=1, le=100000, description="Override max rows for this database"
    )
    max_execution_time: float | None = Field(
        default=None, ge=1.0, le=300.0, description="Override query timeout for this database"
    )
    readonly_role: str | None = Field(
        default=None, description="Override the read-only role used for this database"
    )
    safe_search_path: str | None = Field(
        default=None, description="Override the safe search_path for this database"
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_list(cls, v: Any) -> Any:
        """Parse comma-separated strings into lists."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration.

    The global policy defined here is applied to every database. Use
    ``per_database`` (environment variable ``SECURITY_PER_DATABASE``) to add
    extra restrictions for specific databases.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    allow_write_operations: bool = Field(
        default=False, description="Allow write operations (INSERT, UPDATE, DELETE)"
    )
    blocked_functions: EnvList = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: EnvList = Field(
        default_factory=list,
        description=(
            "Tables (optionally schema-qualified, e.g. 'public.users') that queries are "
            "never allowed to touch"
        ),
    )
    blocked_columns: EnvList = Field(
        default_factory=list,
        description=(
            "Columns (optionally qualified as 'table.column' or 'schema.table.column') "
            "that queries are never allowed to read"
        ),
    )
    allow_explain: bool = Field(
        default=False,
        description=(
            "Allow EXPLAIN statements. EXPLAIN is read-only but leaks query plans, so "
            "it is disabled by default."
        ),
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )
    per_database: dict[str, DatabaseSecurityConfig] = Field(
        default_factory=dict,
        description="Per-database security overrides keyed by database name",
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_blocked_list(cls, v: Any) -> Any:
        """Parse comma-separated string or list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("per_database", mode="before")
    @classmethod
    def parse_per_database(cls, v: Any) -> Any:
        """Accept a JSON object or JSON string describing per-database overrides."""
        if isinstance(v, str):
            if not v.strip():
                return {}
            return json.loads(v)
        return v

    def effective_for(self, database: str) -> SecurityConfig:
        """Return the effective security policy for a given database.

        The global policy is the baseline; any override registered for
        ``database`` is merged on top. Blocked functions/tables/columns are
        additive (deduplicated, order preserved), so per-database rules can only
        tighten the global policy.

        Args:
            database: Name of the database to resolve the policy for.

        Returns:
            SecurityConfig: A new, fully resolved policy. The global instance is
            never mutated.

        Example:
            >>> cfg = SecurityConfig(blocked_tables=["secrets"])
            >>> cfg.effective_for("analytics").blocked_tables
            ['secrets']
        """
        override = self.per_database.get(database)
        if override is None:
            return self

        updates: dict[str, Any] = {}

        def _merge(field: str, extra: list[str] | None) -> None:
            if not extra:
                return
            updates[field] = list(dict.fromkeys([*getattr(self, field), *extra]))

        _merge("blocked_functions", override.blocked_functions)
        _merge("blocked_tables", override.blocked_tables)
        _merge("blocked_columns", override.blocked_columns)

        for field in (
            "allow_explain",
            "max_rows",
            "max_execution_time",
            "readonly_role",
            "safe_search_path",
        ):
            value = getattr(override, field)
            if value is not None:
                updates[field] = value

        return self.model_copy(update=updates)


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )
    min_confidence_score: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence score (0-100)"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.0, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    max_retry_delay: float = Field(
        default=30.0, ge=0.0, le=300.0, description="Upper bound for a single retry delay"
    )
    retry_jitter: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the computed delay applied as random jitter (0 disables it). "
            "Jitter avoids retry stampedes against a recovering dependency."
        ),
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )

    # Rate limiting
    rate_limit_enabled: bool = Field(
        default=True, description="Enforce concurrency limits on LLM and database operations"
    )
    max_concurrent_queries: int = Field(
        default=10, ge=1, le=1000, description="Maximum concurrent database operations"
    )
    max_concurrent_llm_calls: int = Field(
        default=5, ge=1, le=1000, description="Maximum concurrent LLM API calls"
    )
    rate_limit_timeout: float = Field(
        default=10.0,
        ge=0.1,
        le=300.0,
        description="Seconds to wait for a concurrency slot before rejecting the request",
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(
        default="json", description="Log format: structured JSON (default) or human-readable text"
    )


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    databases: list[DatabaseConfig] = Field(
        default_factory=list,
        description=(
            "Additional databases addressable through the `database` tool argument. "
            "Configured with the DATABASES environment variable as a JSON array."
        ),
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def validate_databases(self) -> Settings:
        """Ensure database names are unique."""
        names = [self.database.name, *(db.name for db in self.databases)]
        duplicates = sorted({name for name in names if names.count(name) > 1})

        if duplicates:
            raise ValueError(
                f"Duplicate database names configured: {duplicates}. "
                "Each database must have a unique name."
            )
        return self

    def resolved_databases(self) -> list[DatabaseConfig]:
        """Return every database the server should connect to.

        The primary ``database`` always comes first; entries from ``databases``
        that share its name are ignored (the primary wins).

        Returns:
            list[DatabaseConfig]: Ordered, de-duplicated database configurations.

        Example:
            >>> Settings().resolved_databases()[0].name
            'postgres'
        """
        resolved = [self.database]
        seen = {self.database.name}
        for db in self.databases:
            if db.name in seen:
                continue
            seen.add(db.name)
            resolved.append(db)
        return resolved

    def database_names(self) -> list[str]:
        """Return the names of every configured database.

        Returns:
            list[str]: Database names in resolution order.
        """
        return [db.name for db in self.resolved_databases()]

    def get_database(self, name: str) -> DatabaseConfig | None:
        """Look up a database configuration by name.

        Args:
            name: Database name to look up.

        Returns:
            DatabaseConfig | None: The matching configuration, or None.
        """
        for db in self.resolved_databases():
            if db.name == name:
                return db
        return None

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None
