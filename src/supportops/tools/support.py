"""The ten typed tools for the local fictional support environment."""

from typing import Any
from uuid import uuid4

from supportops.config import Settings
from supportops.database import Database
from supportops.errors import ToolExecutionError
from supportops.models import (
    ApplyCreditInput,
    EscalateTicketInput,
    GetAuditLogInput,
    GetCustomerAccountInput,
    GetTicketInput,
    ListTicketsInput,
    PolicyInput,
    ProposeCreditInput,
    SearchTicketsInput,
    ToolResult,
    VerifyCreditInput,
)
from supportops.tools.registry import ToolRegistry


def build_registry(database: Database, settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()

    def list_tickets(args: dict[str, Any], _state: Any) -> ToolResult:
        page, total = database.list_tickets(
            status=args["status"],
            priority=args["priority"],
            category=args["category"],
            page=args["page"],
            page_size=args["page_size"],
        )
        return ToolResult(
            tool_name="list_tickets",
            data={"tickets": page, "total": total, "page": args["page"], "page_size": args["page_size"]},
            message=f"Found {total} matching tickets.",
        )

    def get_ticket(args: dict[str, Any], _state: Any) -> ToolResult:
        ticket = database.get_ticket(args["ticket_id"])
        if not ticket:
            raise ToolExecutionError("Ticket not found.", code="not_found")
        return ToolResult(tool_name="get_ticket", data={"ticket": ticket})

    def search_tickets(args: dict[str, Any], _state: Any) -> ToolResult:
        tickets, total = database.search_tickets(args["query"], args["page"], args["page_size"])
        return ToolResult(
            tool_name="search_tickets",
            data={"tickets": tickets, "total": total, "page": args["page"]},
            message=f"Found {total} matching tickets.",
        )

    def get_customer_account(args: dict[str, Any], _state: Any) -> ToolResult:
        account = database.get_customer_for_ticket(args["ticket_id"])
        if not account:
            raise ToolExecutionError("No customer account is associated with that ticket.", code="not_found")
        return ToolResult(tool_name="get_customer_account", data={"account": account})

    def check_compensation_policy(args: dict[str, Any], _state: Any) -> ToolResult:
        ticket = database.get_ticket(args["ticket_id"])
        if not ticket:
            raise ToolExecutionError("Ticket not found.", code="not_found")
        policy = database.policy_document()
        amount = float(args["amount"])
        eligible_statuses = set(policy["rules"][0]["eligible_statuses"])
        allowed = (
            ticket["category"].lower() == "billing"
            and ticket["status"] in eligible_statuses
            and amount <= float(policy["rules"][0]["max_automatic_amount"])
        )
        rule = "BILLING-01" if allowed else ("BILLING-02" if amount > 100 else "BILLING-03")
        approval_required = allowed and amount > settings.approval_threshold
        explanation = (
            "Credit is within policy but exceeds the configured approval threshold."
            if approval_required
            else (
                "Credit is within policy and below the configured approval threshold."
                if allowed
                else "This case is outside automated billing-credit policy and must be reviewed."
            )
        )
        return ToolResult(
            tool_name="check_compensation_policy",
            data={
                "allowed": allowed,
                "approval_required": approval_required,
                "policy_name": policy["name"],
                "policy_version": policy["version"],
                "rule_id": rule,
                "rule": next(item["summary"] for item in policy["rules"] if item["id"] == rule),
                "amount": amount,
                "approval_threshold": settings.approval_threshold,
                "explanation": explanation,
            },
        )

    def propose_credit(args: dict[str, Any], state: Any) -> ToolResult:
        policy_result = state.collected_context.get("policy", {})
        if not policy_result.get("allowed"):
            raise ToolExecutionError("Policy does not allow a credit proposal for this case.", code="policy_denied")
        ticket = database.get_ticket(args["ticket_id"])
        account = database.get_customer_for_ticket(args["ticket_id"])
        if not ticket or not account:
            raise ToolExecutionError("Ticket or associated account could not be found.", code="not_found")
        if abs(float(args["amount"]) - float(policy_result["amount"])) > 0.005:
            raise ToolExecutionError("Proposed amount does not match the checked policy amount.", code="policy_mismatch")
        proposal_id = f"PROP-{uuid4().hex[:12].upper()}"
        proposal = {
            "proposal_id": proposal_id,
            "run_id": state.run_id,
            "ticket_id": args["ticket_id"],
            "customer_id": account["customer_id"],
            "amount": float(args["amount"]),
            "reason": args["reason"],
            "operation_key": args["operation_key"],
            "approval_required": bool(policy_result["approval_required"]),
            "policy_rule": policy_result["rule_id"],
        }
        database.create_proposal(proposal)
        return ToolResult(
            tool_name="propose_credit",
            data={**proposal, "customer_name": account["customer_name"]},
            message="Credit proposal recorded; the account has not been changed.",
        )

    def apply_credit(args: dict[str, Any], state: Any) -> ToolResult:
        try:
            credit = database.apply_proposal(args["proposal_id"], args["operation_key"], state.run_id)
        except PermissionError as exc:
            raise ToolExecutionError(str(exc), code="approval_required") from exc
        except ValueError as exc:
            raise ToolExecutionError(str(exc), code="invalid_proposal") from exc
        return ToolResult(
            tool_name="apply_credit",
            data=credit,
            message="Credit applied." if credit["applied"] else "Credit already exists; no duplicate was created.",
        )

    def verify_credit(args: dict[str, Any], _state: Any) -> ToolResult:
        result = database.verify_credit(args["customer_id"], float(args["amount"]), args["operation_key"])
        return ToolResult(
            tool_name="verify_credit",
            data=result,
            message="Independent account read confirmed the credit." if result["verified"] else
            "Independent account read did not confirm the expected credit.",
        )

    def escalate_ticket(args: dict[str, Any], state: Any) -> ToolResult:
        if not database.get_ticket(args["ticket_id"]):
            raise ToolExecutionError("Ticket not found.", code="not_found")
        escalation_id = database.create_escalation(state.run_id, args["ticket_id"], args["reason"])
        return ToolResult(
            tool_name="escalate_ticket",
            data={"escalation_id": escalation_id, "ticket_id": args["ticket_id"], "status": "open"},
            message="Ticket escalated for specialist review.",
        )

    def get_audit_log(args: dict[str, Any], _state: Any) -> ToolResult:
        return ToolResult(
            tool_name="get_audit_log",
            data={"entries": database.get_audit(args["run_id"])},
            message="Audit evidence retrieved.",
        )

    registry.register("list_tickets", ListTicketsInput, list_tickets)
    registry.register("get_ticket", GetTicketInput, get_ticket)
    registry.register("search_tickets", SearchTicketsInput, search_tickets)
    registry.register("get_customer_account", GetCustomerAccountInput, get_customer_account)
    registry.register("check_compensation_policy", PolicyInput, check_compensation_policy)
    registry.register("propose_credit", ProposeCreditInput, propose_credit)
    registry.register("apply_credit", ApplyCreditInput, apply_credit)
    registry.register("verify_credit", VerifyCreditInput, verify_credit)
    registry.register("escalate_ticket", EscalateTicketInput, escalate_ticket)
    registry.register("get_audit_log", GetAuditLogInput, get_audit_log)
    return registry