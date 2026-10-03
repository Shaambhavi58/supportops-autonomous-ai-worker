"""Explicit state-transition guard for persisted agent runs."""

from supportops.errors import SupportOpsError
from supportops.models import RunState, RunStatus

_ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PLANNING: {RunStatus.EXECUTING, RunStatus.FAILED},
    RunStatus.EXECUTING: {
        RunStatus.WAITING_FOR_APPROVAL, RunStatus.VERIFYING, RunStatus.FAILED,
        RunStatus.NEEDS_HUMAN_INPUT,
    },
    RunStatus.WAITING_FOR_APPROVAL: {
        RunStatus.EXECUTING, RunStatus.NEEDS_HUMAN_INPUT, RunStatus.FAILED,
    },
    RunStatus.VERIFYING: {RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.NEEDS_HUMAN_INPUT: set(),
}


class StateMachine:
    @staticmethod
    def transition(state: RunState, target: RunStatus) -> None:
        if target == state.status:
            return
        if target not in _ALLOWED[state.status]:
            raise SupportOpsError(f"Invalid run transition: {state.status.value} → {target.value}.")
        state.status = target