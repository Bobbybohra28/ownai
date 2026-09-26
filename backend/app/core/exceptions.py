"""Error hierarchy.

Every error carries a stable machine-readable ``code``, a user-facing ``message``
(safe to show, no secrets or stack traces) and optional technical ``detail`` that is
logged but never returned to clients.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # generic
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    # auth
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    # models
    MODEL_CONNECTION_ERROR = "MODEL_CONNECTION_ERROR"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_HTTP_ERROR = "MODEL_HTTP_ERROR"
    MODEL_AUTH_ERROR = "MODEL_AUTH_ERROR"
    MODEL_INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"
    MODEL_EMPTY_RESPONSE = "MODEL_EMPTY_RESPONSE"
    MODEL_WRONG_NAME = "MODEL_WRONG_NAME"
    MODEL_STREAM_ERROR = "MODEL_STREAM_ERROR"
    MODEL_CONTEXT_LENGTH_EXCEEDED = "MODEL_CONTEXT_LENGTH_EXCEEDED"
    MODEL_CAPABILITY_UNSUPPORTED = "MODEL_CAPABILITY_UNSUPPORTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    # pipeline
    AGENT_EMPTY_RESPONSE = "AGENT_EMPTY_RESPONSE"
    AGENT_INVALID_OUTPUT = "AGENT_INVALID_OUTPUT"
    AGENT_FAILED = "AGENT_FAILED"
    ORCHESTRATOR_EMPTY_RESPONSE = "ORCHESTRATOR_EMPTY_RESPONSE"
    ORCHESTRATOR_ERROR = "ORCHESTRATOR_ERROR"
    PLAN_INVALID = "PLAN_INVALID"
    RUN_CANCELLED = "RUN_CANCELLED"
    RUN_BUDGET_EXCEEDED = "RUN_BUDGET_EXCEEDED"
    FRONTEND_RESPONSE_ERROR = "FRONTEND_RESPONSE_ERROR"
    # tools / sandbox / data
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TOOL_INVALID_ARGS = "TOOL_INVALID_ARGS"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    SANDBOX_FAILED = "SANDBOX_FAILED"
    SQL_NOT_ALLOWED = "SQL_NOT_ALLOWED"
    DATABASE_ERROR = "DATABASE_ERROR"
    RAG_ERROR = "RAG_ERROR"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    VECTOR_STORE_UNAVAILABLE = "VECTOR_STORE_UNAVAILABLE"
    IMPORT_FAILED = "IMPORT_FAILED"
    CHANGESET_CONFLICT = "CHANGESET_CONFLICT"
    GIT_ERROR = "GIT_ERROR"


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.PATH_NOT_ALLOWED: 403,
    ErrorCode.COMMAND_NOT_ALLOWED: 403,
    ErrorCode.SQL_NOT_ALLOWED: 403,
    ErrorCode.TOOL_NOT_ALLOWED: 403,
    ErrorCode.TOOL_INVALID_ARGS: 422,
    ErrorCode.TOOL_NOT_FOUND: 404,
    ErrorCode.APPROVAL_REQUIRED: 409,
    ErrorCode.CHANGESET_CONFLICT: 409,
    ErrorCode.PLAN_INVALID: 422,
    ErrorCode.IMPORT_FAILED: 422,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.SANDBOX_UNAVAILABLE: 503,
    ErrorCode.VECTOR_STORE_UNAVAILABLE: 503,
    ErrorCode.MODEL_CONNECTION_ERROR: 502,
    ErrorCode.MODEL_TIMEOUT: 504,
    ErrorCode.MODEL_HTTP_ERROR: 502,
    ErrorCode.MODEL_AUTH_ERROR: 502,
    ErrorCode.MODEL_INVALID_RESPONSE: 502,
    ErrorCode.MODEL_EMPTY_RESPONSE: 502,
    ErrorCode.MODEL_WRONG_NAME: 502,
    ErrorCode.MODEL_STREAM_ERROR: 502,
    ErrorCode.CONFIGURATION_ERROR: 500,
}


class AppError(Exception):
    """Base class for all expected, user-explainable errors."""

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        detail: str | None = None,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.message = message
        self.detail = detail
        self.hint = hint
        self.data = data or {}
        self.status_code = status_code or _STATUS.get(self.code, 500)

    def to_public(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.data:
            body["data"] = self.data
        return body

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" + (f" ({self.detail})" if self.detail else "")


class NotFoundError(AppError):
    default_code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    default_code = ErrorCode.CONFLICT


class ValidationFailed(AppError):
    default_code = ErrorCode.VALIDATION_ERROR


class AuthenticationError(AppError):
    default_code = ErrorCode.AUTHENTICATION_REQUIRED


class PermissionDenied(AppError):
    default_code = ErrorCode.PERMISSION_DENIED


class QuotaExceeded(AppError):
    default_code = ErrorCode.QUOTA_EXCEEDED


class ConfigurationError(AppError):
    default_code = ErrorCode.CONFIGURATION_ERROR


class ModelError(AppError):
    """Raised by model providers. ``retryable`` errors allow the router to fall back."""

    default_code = ErrorCode.MODEL_HTTP_ERROR
    _RETRYABLE = {
        ErrorCode.MODEL_CONNECTION_ERROR,
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.MODEL_HTTP_ERROR,
        ErrorCode.MODEL_EMPTY_RESPONSE,
        ErrorCode.MODEL_INVALID_RESPONSE,
        ErrorCode.MODEL_WRONG_NAME,
        ErrorCode.MODEL_STREAM_ERROR,
        ErrorCode.MODEL_AUTH_ERROR,
        ErrorCode.MODEL_UNAVAILABLE,
    }

    def __init__(self, message: str, *, model_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.model_id = model_id

    @property
    def retryable(self) -> bool:
        return self.code in self._RETRYABLE


class AgentError(AppError):
    default_code = ErrorCode.AGENT_FAILED


class OrchestratorError(AppError):
    default_code = ErrorCode.ORCHESTRATOR_ERROR


class ToolError(AppError):
    default_code = ErrorCode.TOOL_FAILED


class SandboxError(AppError):
    default_code = ErrorCode.SANDBOX_FAILED


class RAGError(AppError):
    default_code = ErrorCode.RAG_ERROR
