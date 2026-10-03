import pytest

from supportops.config import Settings
from supportops.errors import SupportOpsError
from supportops.models import RunState, RunStatus
from supportops.state_machine import StateMachine


def test_state_machine_rejects_completion_before_verification():
    state = RunState(run_id="state-test", original_goal="Verify first")
    StateMachine.transition(state, RunStatus.EXECUTING)
    with pytest.raises(SupportOpsError):
        StateMachine.transition(state, RunStatus.COMPLETED)


def test_environment_config_validates_retry_limits(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "11")
    with pytest.raises(ValueError, match="MAX_RETRIES"):
        Settings.from_env()


def test_non_demo_mode_requires_optional_llm_key(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Settings.from_env()