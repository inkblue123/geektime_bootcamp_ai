"""Query request and response models.

This module defines data models for query requests from clients and
responses containing query results or errors.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReturnType(StrEnum):
    """Type of return value requested by the client."""

    SQL = "sql"  # Return only the generated SQL
    RESULT = "result"  # Execute and return query results


class QueryRequest(BaseModel):
    """Query request from client containing natural language question."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="Natural language question about the data",
    )
    database: str | None = Field(
        None, description="Target database name (uses default if not specified)"
    )
    return_type: ReturnType = Field(
        default=ReturnType.RESULT, description="Whether to return SQL or execute and return results"
    )

    @field_validator("question")
    @classmethod
    def sanitize_question(cls, v: str) -> str:
        """Sanitize and validate the question.

        Args:
            v: The input question string.

        Returns:
            str: Sanitized question.

        Raises:
            ValueError: If question is empty after stripping.
        """
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty")
        return v


class ValidationResult(BaseModel):
    """Result of SQL validation checks."""

    is_valid: bool = Field(..., description="Whether SQL passed validation")
    is_select: bool = Field(default=False, description="Whether SQL is a SELECT statement")
    allows_data_modification: bool = Field(
        default=False, description="Whether SQL contains write operations"
    )
    uses_blocked_functions: list[str] = Field(
        default_factory=list, description="List of blocked functions found in SQL"
    )
    error_message: str | None = Field(None, description="Validation error message if invalid")
    error_code: str | None = Field(
        None,
        description=(
            "Machine-readable error code when `is_valid` is False, mirroring "
            "`pg_mcp.models.errors.ErrorCode`."
        ),
    )

    @property
    def is_safe(self) -> bool:
        """Check if SQL is safe to execute.

        Returns:
            bool: True if SQL is safe (valid, read-only, no blocked functions).
        """
        return (
            self.is_valid
            and self.is_select
            and not self.allows_data_modification
            and not self.uses_blocked_functions
        )


class ResultValidationResult(BaseModel):
    """Result of query result validation using LLM.

    This model represents the outcome of validating whether query results
    match the user's original question. It uses LLM to assess the semantic
    correctness and relevance of the results.
    """

    confidence: int = Field(
        ...,
        ge=0,
        le=100,
        description="Confidence score (0-100) that results match the question",
    )
    explanation: str = Field(..., description="Explanation of the validation assessment")
    suggestion: str | None = Field(None, description="Optional suggestion for improving the query")
    is_acceptable: bool = Field(
        ..., description="Whether results are acceptable based on confidence threshold"
    )


class QueryResult(BaseModel):
    """Result data from query execution."""

    columns: list[str] = Field(default_factory=list, description="Column names in result set")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="Result rows as dicts")
    row_count: int = Field(default=0, ge=0, description="Number of rows returned")
    total_row_count: int = Field(
        default=0,
        ge=0,
        description="Total number of rows matched before the max_rows limit was applied",
    )
    truncated: bool = Field(
        default=False, description="Whether the result set was truncated by the row limit"
    )
    execution_time_ms: float = Field(default=0.0, ge=0.0, description="Query execution time in ms")

    @field_validator("row_count", mode="before")
    @classmethod
    def validate_row_count(cls, v: int, info: Any) -> int:
        """Ensure row_count matches length of rows.

        Args:
            v: The row count value.
            info: Validation info containing other fields.

        Returns:
            int: Validated row count.
        """
        # If rows exist in values, use its length
        if hasattr(info, "data") and "rows" in info.data:
            return len(info.data["rows"])
        return v

    @field_validator("total_row_count", mode="before")
    @classmethod
    def default_total_row_count(cls, v: int | None, info: Any) -> int:
        """Default ``total_row_count`` to the number of returned rows."""
        if (v is None or v == 0) and hasattr(info, "data") and "rows" in info.data:
            return len(info.data["rows"])
        return v or 0

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary.

        Returns:
            dict: Dictionary representation of query result.
        """
        return self.model_dump()


class ErrorDetail(BaseModel):
    """Detailed error information."""

    code: str = Field(..., description="Error code identifier")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] | None = Field(None, description="Additional error context")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation.

        Returns:
            dict: Dictionary containing error information. ``details`` is omitted
            when empty so that clients are not handed a meaningless null.
        """
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class QueryResponse(BaseModel):
    """Complete query response to client."""

    success: bool = Field(..., description="Whether query succeeded")
    request_id: str | None = Field(
        None, description="Request identifier used for end-to-end tracing"
    )
    database: str | None = Field(
        None, description="Database the query actually ran against (after resolution)"
    )
    generated_sql: str | None = Field(None, description="Generated SQL query")
    validation: ValidationResult | None = Field(None, description="SQL validation results")
    data: QueryResult | None = Field(None, description="Query result data (if executed)")
    result_validation: ResultValidationResult | None = Field(
        None, description="LLM assessment of whether the results answer the question"
    )
    error: ErrorDetail | None = Field(None, description="Error information if failed")
    confidence: int = Field(
        default=100, ge=0, le=100, description="Confidence score of generated SQL (0-100)"
    )
    low_confidence: bool = Field(
        default=False,
        description=(
            "True when the result validation confidence is below "
            "ValidationConfig.min_confidence_score"
        ),
    )
    tokens_used: int | None = Field(None, ge=0, description="LLM tokens used for generation")

    @field_validator("data")
    @classmethod
    def validate_data(cls, v: QueryResult | None, info: Any) -> QueryResult | None:
        """Ensure data is present only when success is True and executed.

        Args:
            v: The data field value.
            info: Validation info containing other fields.

        Returns:
            QueryResult | None: Validated data field.
        """
        if hasattr(info, "data"):
            success = info.data.get("success", False)
            error = info.data.get("error")
            if not success and v is not None:
                raise ValueError("Data should not be present when success is False")
            if success and error is not None and v is not None:
                raise ValueError("Cannot have both data and error")
        return v

    @field_validator("error")
    @classmethod
    def validate_error(cls, v: ErrorDetail | None, info: Any) -> ErrorDetail | None:
        """Ensure error is present when success is False.

        Args:
            v: The error field value.
            info: Validation info containing other fields.

        Returns:
            ErrorDetail | None: Validated error field.
        """
        if hasattr(info, "data"):
            success = info.data.get("success", False)
            if not success and v is None:
                raise ValueError("Error must be present when success is False")
        return v

    def to_dict(self) -> dict[str, Any]:
        """Convert response to dictionary for the MCP tool return value.

        `tokens_used` is always emitted (as ``0`` when unknown) so that MCP
        clients can rely on the field being present regardless of whether the
        request reached the LLM.

        Returns:
            dict: Dictionary representation compatible with the MCP protocol.
        """
        result = self.model_dump(exclude_none=False)
        if result.get("tokens_used") is None:
            result["tokens_used"] = 0
        return result
