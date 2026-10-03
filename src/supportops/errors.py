"""Safe, explicit errors used by the worker and its tool boundary."""


class SupportOpsError(Exception):
    """Base exception with a user-safe message."""


class ToolExecutionError(SupportOpsError):
    def __init__(self, message: str, *, code: str = "tool_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ToolValidationError(ToolExecutionError):
    def __init__(self, message: str):
        super().__init__(message, code="invalid_tool_input")


class LLMPlanningError(SupportOpsError):
    """The optional planner did not return a valid, safe plan."""