"""Unit tests for security policy enforcement (blocked tables/columns, EXPLAIN).

These complement ``test_sql_validator.py`` by exercising the policy path that is
actually wired into the server: a :class:`SecurityConfig` instance drives the
validator, and ``analyze()`` reports truthful findings without raising.
"""

from __future__ import annotations

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import ErrorCode, SecurityViolationError
from pg_mcp.services.sql_validator import SQLValidator


def validator(**kwargs: object) -> SQLValidator:
    """Build a validator from explicit SecurityConfig fields."""
    return SQLValidator(SecurityConfig(**kwargs))  # type: ignore[arg-type]


class TestBlockedTables:
    """Blocked table enforcement."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM secret_data",
            "SELECT * FROM public.secret_data",
            "SELECT s.api_key FROM secret_data s",
            "SELECT * FROM users JOIN secret_data ON users.id = secret_data.user_id",
            "WITH leaked AS (SELECT * FROM secret_data) SELECT * FROM leaked",
        ],
    )
    def test_blocked_table_is_rejected(self, sql: str) -> None:
        v = validator(blocked_tables=["secret_data"])
        with pytest.raises(SecurityViolationError, match="secret_data"):
            v.validate_or_raise(sql)

    def test_fully_qualified_entry_matches(self) -> None:
        v = validator(blocked_tables=["public.secret_data"])
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise("SELECT * FROM public.secret_data")

    def test_unrelated_table_is_allowed(self) -> None:
        v = validator(blocked_tables=["secret_data"])
        assert v.validate("SELECT * FROM users") == (True, None)


class TestBlockedColumns:
    """Blocked column enforcement."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT password_hash FROM users",
            "SELECT u.password_hash FROM users u",
            "SELECT * FROM users WHERE password_hash IS NOT NULL",
        ],
    )
    def test_bare_column_entry_is_global(self, sql: str) -> None:
        v = validator(blocked_columns=["password_hash"])
        with pytest.raises(SecurityViolationError, match="password_hash"):
            v.validate_or_raise(sql)

    def test_table_qualified_entry_matches_qualified_use(self) -> None:
        v = validator(blocked_columns=["users.password_hash"])
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise("SELECT users.password_hash FROM users")
        # A bare reference is not blocked by a table-qualified rule
        assert v.validate("SELECT password_hash FROM users") == (True, None)

    def test_schema_qualified_entry_matches(self) -> None:
        v = validator(blocked_columns=["public.users.password_hash"])
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise("SELECT public.users.password_hash FROM public.users")

    def test_other_columns_are_allowed(self) -> None:
        v = validator(blocked_columns=["password_hash"])
        assert v.validate("SELECT username, email FROM users") == (True, None)


class TestExplainPolicy:
    """EXPLAIN is denied unless explicitly enabled."""

    def test_explain_denied_by_default(self) -> None:
        v = validator()
        with pytest.raises(SecurityViolationError, match="EXPLAIN"):
            v.validate_or_raise("EXPLAIN SELECT * FROM users")

    def test_explain_allowed_by_config(self) -> None:
        v = validator(allow_explain=True)
        assert v.validate("EXPLAIN SELECT * FROM users") == (True, None)

    def test_explicit_override_beats_config(self) -> None:
        v = SQLValidator(SecurityConfig(allow_explain=True), allow_explain=False)
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise("EXPLAIN SELECT * FROM users")


class TestWritePolicy:
    """`allow_write_operations` gates INSERT/UPDATE/DELETE but never DDL."""

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users (id) VALUES (1)",
            "UPDATE users SET name = 'x'",
            "DELETE FROM users",
        ],
    )
    def test_writes_denied_by_default(self, sql: str) -> None:
        v = validator()
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users (id) VALUES (1)",
            "UPDATE users SET name = 'x'",
            "DELETE FROM users",
        ],
    )
    def test_writes_allowed_when_enabled(self, sql: str) -> None:
        v = validator(allow_write_operations=True)
        assert v.validate(sql) == (True, None)

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "CREATE TABLE t (id int)",
            "ALTER TABLE users ADD COLUMN x int",
            "GRANT ALL ON users TO public",
        ],
    )
    def test_ddl_always_denied(self, sql: str) -> None:
        v = validator(allow_write_operations=True)
        with pytest.raises(SecurityViolationError):
            v.validate_or_raise(sql)


class TestAnalyze:
    """`analyze()` reports findings without raising."""

    def test_valid_statement(self) -> None:
        result = validator().analyze("SELECT id FROM users")
        assert result.is_valid is True
        assert result.is_select is True
        assert result.allows_data_modification is False
        assert result.uses_blocked_functions == []
        assert result.error_code is None
        assert result.is_safe is True

    def test_blocked_table_reports_reason(self) -> None:
        result = validator(blocked_tables=["secret_data"]).analyze("SELECT * FROM secret_data")
        assert result.is_valid is False
        assert result.error_code == str(ErrorCode.SECURITY_VIOLATION)
        assert "secret_data" in result.error_message

    def test_parse_error_reports_parse_code(self) -> None:
        result = validator().analyze("SELECT FROM WHERE")
        assert result.is_valid is False
        assert result.error_code in (
            str(ErrorCode.SQL_PARSE_ERROR),
            str(ErrorCode.SECURITY_VIOLATION),
        )

    def test_empty_sql_reports_parse_error(self) -> None:
        result = validator().analyze("   ")
        assert result.is_valid is False
        assert result.error_code == str(ErrorCode.SQL_PARSE_ERROR)

    def test_write_statement_is_flagged_as_modifying(self) -> None:
        result = validator().analyze("DELETE FROM users")
        assert result.is_valid is False
        assert result.allows_data_modification is True

    def test_blocked_function_is_reported(self) -> None:
        result = validator().analyze("SELECT pg_sleep(1)")
        assert result.is_valid is False
        assert "pg_sleep" in result.uses_blocked_functions

    def test_schema_qualified_blocked_function_is_reported(self) -> None:
        result = validator().analyze("SELECT pg_catalog.pg_sleep(1)")
        assert result.is_valid is False
        assert "pg_sleep" in result.uses_blocked_functions

    def test_explain_is_read_only_when_allowed(self) -> None:
        result = validator(allow_explain=True).analyze("EXPLAIN SELECT * FROM users")
        assert result.is_valid is True
        assert result.is_select is True

    def test_multiple_statements_rejected(self) -> None:
        result = validator().analyze("SELECT 1; DROP TABLE users;")
        assert result.is_valid is False
