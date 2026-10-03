"""Tool registry enforcing a strict typed, whitelisted execution boundary."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from supportops.errors import ToolExecutionError, ToolValidationError
from supportops.models import RunState, ToolResult

ToolHandler = Callable[[dict[str, Any], RunState], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[type[BaseModel], ToolHandler]] = {}

    def register(self, name: str, input_model: type[BaseModel], handler: ToolHandler) -> None:
        if not name or name in self._tools:
            raise ValueError(f"Tool name is empty or already registered: {name}")
        self._tools[name] = (input_model, handler)

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def execute(self, name: str, payload: dict[str, Any], state: RunState) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolValidationError(f"Tool '{name}' is not registered.")
        input_model, handler = spec
        try:
            validated = input_model.model_validate(payload)
        except ValidationError as exc:
            details = "; ".join(error["msg"] for error in exc.errors(include_input=False))
            raise ToolValidationError(f"Invalid input for {name}: {details}") from exc
        try:
            result = handler(validated.model_dump(mode="json"), state)
        except ToolExecutionError:
            raise
        except KeyError as exc:
            raise ToolExecutionError("The requested fictional record was not found.", code="not_found") from exc
        except (PermissionError, ValueError) as exc:
            raise ToolExecutionError(str(exc), code="policy_or_state_rejected") from exc
        if not isinstance(result, ToolResult) or result.tool_name != name:
            raise ToolExecutionError(f"Tool {name} returned an invalid result.", code="invalid_result")
        return result