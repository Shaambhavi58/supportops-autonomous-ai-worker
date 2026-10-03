from supportops.models import RunStatus


def _goal(amount: int = 75) -> str:
    return (
        f"Find the highest-priority unresolved billing ticket, check if a ${amount} credit is "
        "allowed, request approval if required, apply it, and verify the account."
    )


def _credit_count(database, ticket_id: str = "TCK-1001") -> int:
    with database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM credits WHERE ticket_id=?", (ticket_id,)
        ).fetchone()[0]


def test_deterministic_plan_uses_registered_tools(make_agent):
    agent, _database = make_agent()
    state = agent.run(_goal(75))
    assert [step.tool_name for step in state.generated_plan] == [
        "list_tickets",
        "get_ticket",
        "get_customer_account",
        "check_compensation_policy",
        "propose_credit",
        "apply_credit",
        "verify_credit",
        "get_audit_log",
    ]
    assert {step.tool_name for step in state.generated_plan} <= agent.registry.names


def test_approval_pauses_before_write_then_resumes_same_run(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(75))
    assert state.status == RunStatus.WAITING_FOR_APPROVAL
    assert state.pending_approval is not None
    assert state.current_step == "Approve"
    assert _credit_count(database) == 0

    resumed = agent.resume_approval(state.run_id, approved=True, reason="Billing evidence checked.")
    assert resumed.run_id == state.run_id
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.verification_result["verified"] is True
    assert _credit_count(database) == 1
    assert any(item.startswith("credit:CR-") for item in resumed.evidence)


def test_rejection_does_not_apply_and_escalates(make_agent):
    agent, database = make_agent()
    waiting = agent.run(_goal(75))
    rejected = agent.resume_approval(waiting.run_id, approved=False, reason="Not approved.")
    assert rejected.status == RunStatus.NEEDS_HUMAN_INPUT
    assert "rejected" in rejected.final_summary.lower()
    assert _credit_count(database) == 0
    assert any(item.startswith("escalation:") for item in rejected.evidence)


def test_small_credit_can_complete_without_approval(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(25))
    assert state.status == RunStatus.COMPLETED
    assert state.verification_result["verified"] is True
    assert _credit_count(database) == 1


def test_policy_denial_escalates_without_credit(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(150))
    assert state.status == RunStatus.NEEDS_HUMAN_INPUT
    assert "policy" in state.final_summary.lower()
    assert _credit_count(database) == 0
    assert any(item.startswith("escalation:") for item in state.evidence)


def test_transient_crm_failure_is_logged_and_retried(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(75), simulate_transient_failure=True)
    assert state.status == RunStatus.WAITING_FOR_APPROVAL
    assert state.retry_counts["get_customer_account"] == 1
    calls = [call for call in state.tool_calls if call.tool_name == "get_customer_account"]
    assert [call.status for call in calls] == ["failed", "succeeded"]
    audit = database.get_audit(state.run_id)
    assert any(item["event_type"] == "tool_failed" for item in audit)
    assert any(item["event_type"] == "tool_succeeded" for item in audit)


def test_permanent_failure_exhausts_bounded_retries_safely(make_agent):
    agent, database = make_agent(retries=2)
    state = agent.run(_goal(75), simulate_permanent_failure=True)
    assert state.status == RunStatus.FAILED
    assert state.retry_counts["get_customer_account"] == 2
    assert len([call for call in state.tool_calls if call.tool_name == "get_customer_account"]) == 3
    assert _credit_count(database) == 0
    assert "success" not in state.final_summary.lower()


def test_verification_failure_never_reports_completion(make_agent, monkeypatch):
    agent, database = make_agent()
    monkeypatch.setattr(
        database,
        "verify_credit",
        lambda _customer_id, _amount, _operation_key: {
            "verified": False,
            "credit": None,
            "current_balance": 0,
        },
    )
    state = agent.run(_goal(25))
    assert state.status == RunStatus.FAILED
    assert state.verification_result["verified"] is False
    assert "verification failed" in state.final_summary.lower()


def test_run_persists_all_state_fields_and_audit_evidence(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(75))
    persisted = database.get_run(state.run_id)
    assert persisted is not None
    assert persisted.original_goal == state.original_goal
    assert persisted.parsed_intent.requested_credit == 75
    assert len(persisted.tool_calls) == len(state.tool_calls)
    event_types = {item["event_type"] for item in database.get_audit(state.run_id)}
    assert {"tool_succeeded", "approval_requested"} <= event_types


def test_secret_like_goal_text_is_redacted_from_state_and_audit(make_agent):
    agent, database = make_agent()
    state = agent.run("Apply a $25 credit; api_key=sk-do-not-store-this")
    assert state.status == RunStatus.COMPLETED
    assert "sk-do-not-store-this" not in state.original_goal
    audit_text = str(database.get_audit(state.run_id))
    assert "sk-do-not-store-this" not in audit_text


def test_credit_apply_is_idempotent_and_duplicate_ticket_credit_is_blocked(make_agent):
    agent, database = make_agent()
    state = agent.run(_goal(25))
    proposal = state.collected_context["proposal"]
    first = database.apply_proposal(proposal["proposal_id"], proposal["operation_key"], state.run_id)
    assert first["idempotent_replay"] is True
    assert _credit_count(database) == 1

    other_proposal = {
        "proposal_id": "PROP-DUPLICATE",
        "run_id": "another-run",
        "ticket_id": "TCK-1001",
        "customer_id": "CUS-2001",
        "amount": 10,
        "reason": "Duplicate attempt",
        "operation_key": "another-operation-key",
        "approval_required": False,
        "policy_rule": "BILLING-01",
    }
    try:
        database.create_proposal(other_proposal)
        assert False, "A second credit for one ticket should be blocked."
    except ValueError as exc:
        assert "already been applied" in str(exc)


def test_approval_cannot_be_decided_twice(make_agent):
    agent, _database = make_agent()
    state = agent.run(_goal(75))
    agent.resume_approval(state.run_id, approved=False)
    try:
        agent.resume_approval(state.run_id, approved=True)
        assert False, "A second decision must not be accepted."
    except ValueError as exc:
        assert "not waiting" in str(exc)