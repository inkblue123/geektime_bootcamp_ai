"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.

Policy sources
--------------
All policy comes from :class:`~pg_mcp.config.settings.SecurityConfig`:

* ``blocked_functions`` - merged with :attr:`SQLValidator.BUILTIN_DANGEROUS_FUNCTIONS`
* ``blocked_tables`` - bare (``secret_data``) or schema-qualified (``public.secret_data``)
* ``blocked_columns`` - bare (``password_hash``), table-qualified
  (``users.password_hash``) or schema-qualified (``public.users.password_hash``)
* ``allow_explain`` - whether ``EXPLAIN`` is permitted
* ``allow_write_operations`` - whether INSERT/UPDATE/DELETE/MERGE are permitted
"""

from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import ErrorCode, SecurityViolationError, SQLParseError
from pg_mcp.models.query import ValidationResult

#: Statement types that are always rejected, regardless of configuration.
FORBIDDEN_STATEMENT_TYPES: set[type[exp.Expression]] = {
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Grant,
    exp.Revoke,
    exp.Set,
    exp.Command,
    exp.Use,
}

#: Write statements that are only rejected when ``allow_write_operations`` is False.
WRITE_STATEMENT_TYPES: set[type[exp.Expression]] = {
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
}


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements (plus writes when explicitly enabled)
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    - Enforcing the configured EXPLAIN policy

    Example:
        >>> from pg_mcp.config.settings import SecurityConfig
        >>> config = SecurityConfig(blocked_tables=["secret_data"])
        >>> SQLValidator(config).validate("SELECT * FROM secret_data")
        (False, "Access to table 'public.secret_data' is not allowed")
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar[set[type[exp.Expression]]] = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
    }

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar[set[type[exp.Expression]]] = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.With,
        exp.Subquery,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar[set[str]] = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool | None = None,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration. ``blocked_tables``,
                ``blocked_columns`` and ``allow_explain`` are read from here
                unless explicitly overridden below.
            blocked_tables: Optional override for the blocked table list.
            blocked_columns: Optional override for the blocked column list.
            allow_explain: Optional override for the EXPLAIN policy.
        """
        self.config = config

        tables = config.blocked_tables if blocked_tables is None else blocked_tables
        columns = config.blocked_columns if blocked_columns is None else blocked_columns
        explain = config.allow_explain if allow_explain is None else allow_explain

        self.blocked_tables = {t.lower() for t in tables or []}
        self.blocked_columns = {c.lower() for c in columns or []}
        self.allow_explain = explain
        self.allow_write_operations = config.allow_write_operations

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None:
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements
        if isinstance(statement, exp.Command):
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError(
                        "EXPLAIN statements are not allowed (security.allow_explain is false)"
                    )
                # EXPLAIN is read-only: it only shows a query plan. The inner
                # statement is never executed, so it is safe even for writes.
                return
            raise SecurityViolationError(
                f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
            )

        # Handle CTE (WITH) statements - extract the main query
        if isinstance(statement, exp.With):
            if statement.this is None:
                raise SQLParseError("WITH statement has no main query")
            main_query: exp.Expr = statement.this
        else:
            main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def analyze(self, sql: str) -> ValidationResult:
        """Analyse SQL and return structured findings without raising.

        This is the non-raising counterpart of :meth:`validate_or_raise`. The
        orchestrator uses it so that validation outcomes can be reported
        truthfully in responses, logs and metrics instead of being hardcoded to
        "valid".

        Args:
            sql: SQL query string to analyse.

        Returns:
            ValidationResult: Structured validation outcome, including
            ``error_code`` when the statement was rejected.

        Example:
            >>> from pg_mcp.config.settings import SecurityConfig
            >>> result = SQLValidator(SecurityConfig()).analyze("DELETE FROM users")
            >>> result.is_valid, result.allows_data_modification
            (False, True)
        """
        try:
            self.validate_or_raise(sql)
        except SecurityViolationError as exc:
            return ValidationResult(
                is_valid=False,
                is_select=self._is_read_only(sql),
                allows_data_modification=self._is_write(sql),
                uses_blocked_functions=self._blocked_functions_in(sql),
                error_message=str(exc),
                error_code=str(exc.code),
            )
        except SQLParseError as exc:
            return ValidationResult(
                is_valid=False,
                is_select=False,
                allows_data_modification=False,
                uses_blocked_functions=[],
                error_message=str(exc),
                error_code=str(exc.code),
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ValidationResult(
                is_valid=False,
                error_message=str(exc),
                error_code=str(ErrorCode.INTERNAL_ERROR),
            )

        return ValidationResult(
            is_valid=True,
            is_select=self._is_read_only(sql),
            allows_data_modification=self._is_write(sql),
            uses_blocked_functions=[],
            error_message=None,
            error_code=None,
        )

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e

        return sorted({table.name.lower() for table in parsed.find_all(exp.Table) if table.name})

    # ------------------------------------------------------------------
    # Internal checks
    # ------------------------------------------------------------------

    def _check_statement_type(self, statement: exp.Expr) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for forbidden_type in FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        if not self.allow_write_operations:
            for write_type in WRITE_STATEMENT_TYPES:
                if isinstance(statement, write_type):
                    stmt_name = write_type.__name__.upper()
                    return (
                        f"{stmt_name} statements are not allowed. Only SELECT queries are "
                        "permitted (security.allow_write_operations is false)."
                    )

        allowed: set[type[exp.Expression]] = set(self.ALLOWED_STATEMENT_TYPES)
        if self.allow_write_operations:
            allowed |= WRITE_STATEMENT_TYPES

        if not isinstance(statement, tuple(allowed)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expr) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for name in self._blocked_functions_in_expression(statement):
            return f"Function '{name}' is blocked for security reasons"
        return None

    def _blocked_functions_in(self, sql: str) -> list[str]:
        """Best-effort list of blocked functions referenced by ``sql``."""
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return []
        return self._blocked_functions_in_expression(parsed)

    def _blocked_functions_in_expression(self, statement: exp.Expr) -> list[str]:
        """Collect blocked function names used inside a parsed statement."""
        found: list[str] = []
        for func in statement.find_all(exp.Func):
            for candidate in self._function_names(func):
                if candidate in self.blocked_functions and candidate not in found:
                    found.append(candidate)
        return found

    @staticmethod
    def _function_names(func: exp.Expr) -> set[str]:
        """Return every plausible lower-cased name for a function node.

        Handles schema-qualified calls such as ``pg_catalog.pg_sleep(1)`` which
        SQLGlot parses as an anonymous function whose name embeds the schema.
        """
        names: set[str] = set()
        raw_name = getattr(func, "name", "") or ""
        if raw_name:
            names.add(raw_name.lower())
            names.add(raw_name.lower().split(".")[-1])

        if isinstance(func, exp.Anonymous) and isinstance(func.this, str):
            anonymous = func.this.lower()
            names.add(anonymous)
            names.add(anonymous.split(".")[-1])

        return names

    def _check_blocked_tables(self, statement: exp.Expr) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        for table in statement.find_all(exp.Table):
            table_name = (table.name or "").lower()
            if not table_name:
                continue

            schema_name = (getattr(table, "db", None) or "").lower()
            full_name = f"{schema_name}.{table_name}" if schema_name else table_name

            if table_name in self.blocked_tables or full_name in self.blocked_tables:
                return f"Access to table '{full_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expr) -> str | None:
        """Check for access to blocked columns.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        for column in statement.find_all(exp.Column):
            column_name = (column.name or "").lower()
            if not column_name:
                continue

            table_name = (column.table or "").lower()
            schema_name = (getattr(column, "db", None) or "").lower()

            candidates = [column_name]
            if table_name:
                candidates.append(f"{table_name}.{column_name}")
                if schema_name:
                    candidates.append(f"{schema_name}.{table_name}.{column_name}")

            for candidate in candidates:
                if candidate in self.blocked_columns:
                    return f"Access to column '{candidate}' is not allowed"

        return None

    def _check_subquery_safety(self, statement: exp.Expr) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this is None:
                continue
            inner_stmt = subquery.this

            for forbidden_type in FORBIDDEN_STATEMENT_TYPES:
                if isinstance(inner_stmt, forbidden_type):
                    stmt_name = forbidden_type.__name__.upper()
                    return f"{stmt_name} statements in subqueries are not allowed"

            if not self.allow_write_operations:
                for write_type in WRITE_STATEMENT_TYPES:
                    if isinstance(inner_stmt, write_type):
                        stmt_name = write_type.__name__.upper()
                        return f"{stmt_name} statements in subqueries are not allowed"

            if not isinstance(inner_stmt, (exp.Select, exp.With)):
                return "Subqueries must contain only SELECT statements"

        return None

    # ------------------------------------------------------------------
    # Helpers for `analyze`
    # ------------------------------------------------------------------

    @staticmethod
    def _is_write(sql: str) -> bool:
        """Best-effort check for whether ``sql`` modifies data."""
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return False
        if parsed is None:  # pragma: no cover - defensive
            return False

        write_types = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create)
        if isinstance(parsed, write_types):
            return True
        return next(parsed.find_all(*write_types), None) is not None

    @staticmethod
    def _is_read_only(sql: str) -> bool:
        """Best-effort check for whether ``sql`` is a read-only statement."""
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return False
        if parsed is None:  # pragma: no cover - defensive
            return False

        if isinstance(parsed, exp.Command):
            cmd = str(parsed.this).upper() if parsed.this else ""
            return cmd.startswith("EXPLAIN")
        if isinstance(parsed, exp.With):
            inner = parsed.this
            return isinstance(inner, tuple(SQLValidator.ALLOWED_STATEMENT_TYPES))
        return isinstance(parsed, tuple(SQLValidator.ALLOWED_STATEMENT_TYPES))
