from supportops.errors import ToolValidationError
from supportops.models import RunState


def _state():
    return RunState(run_id="tool-test-run", original_goal="Inspect a ticket")


def test_list_tickets_filters_and_paginates(make_agent):
    agent, _database = make_agent()
    result = agent.registry.execute(
        "list_tickets",
        {"status": "open", "priority": None, "category": "billing", "page": 1, "page_size": 1},
        _state(),
    )
    assert result.data["total"] == 3
    assert len(result.data["tickets"]) == 1
    assert result.data["tickets"][0]["ticket_id"] == "TCK-1001"
    page_two = agent.registry.execute(
        "list_tickets",
        {"status": "open", "priority": None, "category": "billing", "page": 2, "page_size": 1},
        _state(),
    )
    assert page_two.data["tickets"][0]["ticket_id"] == "TCK-1010"
    page_three = agent.registry.execute(
        "list_tickets",
        {"status": "open", "priority": "medium", "category": "billing", "page": 1, "page_size": 1},
        _state(),
    )
    assert page_three.data["total"] == 1
    assert page_three.data["tickets"][0]["ticket_id"] == "TCK-1005"


def test_get_ticket_returns_fictional_ticket(make_agent):
    agent, _database = make_agent()
    result = agent.registry.execute("get_ticket", {"ticket_id": "TCK-1001"}, _state())
    assert result.data["ticket"]["subject"] == "Duplicate charge on annual plan"


def test_get_ticket_missing_record_is_an_explicit_tool_error(make_agent):
    agent, _database = make_agent()
    result = None
    try:
        result = agent.registry.execute("get_ticket", {"ticket_id": "MISSING"}, _state())
    except Exception as exc:
        assert "not found" in str(exc).lower()
    assert result is None


def test_search_tickets_matches_subject_description_and_tags(make_agent):
    agent, _database = make_agent()
    result = agent.registry.execute(
        "search_tickets", {"query": "duplicate-charge", "page": 1, "page_size": 10}, _state()
    )
    assert result.data["total"] == 1
    assert result.data["tickets"][0]["ticket_id"] == "TCK-1001"


def test_customer_lookup_uses_ticket_relationship(make_agent):
    agent, _database = make_agent()
    result = agent.registry.execute(
        "get_customer_account", {"ticket_id": "TCK-1001"}, _state()
    )
    assert result.data["account"]["customer_id"] == "CUS-2001"
    assert result.data["account"]["customer_name"] == "Avery Chen"
    assert len(result.data["account"]["previous_credits"]) == 1


def test_policy_allows_eligible_credit_and_requires_threshold_approval(make_agent):
    agent, _database = make_agent(threshold=50)
    allowed = agent.registry.execute(
        "check_compensation_policy", {"ticket_id": "TCK-1001", "amount": 75}, _state()
    )
    assert allowed.data["allowed"] is True
    assert allowed.data["approval_required"] is True
    assert allowed.data["rule_id"] == "BILLING-01"

    denied = agent.registry.execute(
        "check_compensation_policy", {"ticket_id": "TCK-1001", "amount": 150}, _state()
    )
    assert denied.data["allowed"] is False
    assert denied.data["rule_id"] == "BILLING-02"


def test_policy_allows_subthreshold_credit_without_approval(make_agent):
    agent, _database = make_agent(threshold=50)
    result = agent.registry.execute(
        "check_compensation_policy", {"ticket_id": "TCK-1001", "amount": 25}, _state()
    )
    assert result.data["allowed"] is True
    assert result.data["approval_required"] is False


def test_tool_registry_rejects_unknown_tools_and_extra_input(make_agent):
    agent, _database = make_agent()
    try:
        agent.registry.execute("run_shell_command", {"command": "echo unsafe"}, _state())
        assert False, "Unregistered tool should be rejected."
    except ToolValidationError as exc:
        assert "not registered" in str(exc)
    try:
        agent.registry.execute(
            "get_ticket",
            {"ticket_id": "TCK-1001", "url": "https://example.com"},
            _state(),
        )
        assert False, "Unexpected tool input should be rejected."
    except ToolValidationError as exc:
        assert "Invalid input" in str(exc)