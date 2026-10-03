"""Transparent support-resolution agent with resumable approval checkpoints."""

import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from supportops.config import Settings, get_settings
from supportops.database import Database
from supportops.errors import LLMPlanningError, ToolExecutionError
from supportops.logging_utils import log_event, redact_text, sanitize
from supportops.models import (
    PendingApproval,
    RunState,
    RunStatus,
    TimelineEvent,
    ToolCallRecord,
    ToolObservation,
    ToolResult,
)
from supportops.planner import Planner, parse_intent
from supportops.state_machine import StateMachine
from supportops.tools import ToolRegistry, build_registry

_TIMELINE = ("Understand", "Plan", "Execute", "Observe", "Adapt", "Approve", "Verify", "Complete")
_PRIORITY = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
_FAILURE_TARGETS = {"get_customer_account", "apply_credit"}


class SupportOpsAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        database: Database | None = None,
        planner: Planner | None = None,
        registry: ToolRegistry | None = None,
    ):
        self.settings = settings or get_settings()
        self.database = database or Database(
            self.settings.db_path, approval_threshold=self.settings.approval_threshold
        )
        self.registry = registry or build_registry(self.database, self.settings)
        self.planner = planner or Planner(self.settings, self.registry.names)

    def run(
        self,
        goal: str,
        *,
        simulate_transient_failure: bool = False,
        simulate_permanent_failure: bool = False,
    ) -> RunState:
        goal = redact_text(goal.strip())
        if not goal or len(goal) > 2000:
            raise ValueError("Enter a task between 1 and 2,000 characters.")
        if simulate_transient_failure and simulate_permanent_failure:
            raise ValueError("Choose either transient or permanent failure simulation, not both.")
        state = RunState(
            run_id=uuid4().hex,
            original_goal=goal,
            simulate_transient_failure=simulate_transient_failure,
            simulate_permanent_failure=simulate_permanent_failure,
            expected_outcome="Apply only a policy-eligible credit, then independently verify it.",
            timeline=[TimelineEvent(name=name, status="pending") for name in _TIMELINE],
        )
        self.database.save_run(state)
        self._mark(state, "Understand", "completed", "Goal captured and structured.")
        state.parsed_intent = parse_intent(goal)
        self._mark(state, "Plan", "in_progress", "Preparing a tool-whitelisted plan.")
        try:
            state.generated_plan = self.planner.plan(goal, state.parsed_intent)
        except LLMPlanningError as exc:
            self._fail(state, str(exc))
            return state
        self._mark(
            state,
            "Plan",
            "completed",
            " → ".join(step.tool_name for step in state.generated_plan),
        )
        StateMachine.transition(state, RunStatus.EXECUTING)
        self._save(state)
        return self._execute_resolution(state)

    def resume_approval(
        self,
        run_id: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> RunState:
        state = self.database.get_run(run_id)
        if not state:
            raise ValueError("Run not found.")
        if state.status != RunStatus.WAITING_FOR_APPROVAL or not state.pending_approval:
            raise ValueError("This run is not waiting for a decision.")
        pending = state.pending_approval
        approval_id = self.database.record_approval(run_id, pending.proposal_id, approved, reason)
        audit_id = self.database.audit(
            run_id,
            "human_approval_decision",
            "human",
            {
                "approval_id": approval_id,
                "proposal_id": pending.proposal_id,
                "decision": "approved" if approved else "rejected",
                "reason": reason,
            },
        )
        state.evidence.append(f"audit:{audit_id}")
        state.pending_approval = None
        self._mark(
            state,
            "Approve",
            "completed" if approved else "blocked",
            "Human approved the credit." if approved else "Human rejected the credit; no account change was made.",
        )
        log_event("human_approval_decision", run_id=run_id, decision="approved" if approved else "rejected")
        if not approved:
            escalation = self._invoke(
                state,
                "escalate_ticket",
                {
                    "ticket_id": pending.ticket_id,
                    "reason": "Credit was rejected by a human reviewer. Route the billing case to a specialist.",
                },
            )
            if escalation:
                state.evidence.append(f"escalation:{escalation.data['escalation_id']}")
                self._finish_needs_human(
                    state,
                    "Credit rejected. No account change was made; the ticket was escalated for specialist review.",
                )
            self._save(state)
            return state
        StateMachine.transition(state, RunStatus.EXECUTING)
        self._save(state)
        return self._apply_and_verify(state)

    def _execute_resolution(self, state: RunState) -> RunState:
        self._mark(state, "Execute", "in_progress", "Running read-only ticket and account tools first.")
        listing = self._invoke(
            state,
            "list_tickets",
            {"status": None, "priority": None, "category": "billing", "page": 1, "page_size": 100},
        )
        if not listing:
            return state
        unresolved = [
            ticket
            for ticket in listing.data["tickets"]
            if ticket["status"] in {"open", "pending"}
        ]
        if not unresolved:
            self._mark(state, "Adapt", "completed", "No unresolved billing ticket was found.")
            self._finish_needs_human(state, "No unresolved billing ticket matched the task.")
            return state
        ticket = min(
            unresolved,
            key=lambda item: (_PRIORITY.get(item["priority"], 99), item["ticket_id"]),
        )
        ticket_result = self._invoke(state, "get_ticket", {"ticket_id": ticket["ticket_id"]})
        if not ticket_result:
            return state
        state.collected_context["ticket"] = ticket_result.data["ticket"]
        account_result = self._invoke(
            state, "get_customer_account", {"ticket_id": ticket["ticket_id"]}
        )
        if not account_result:
            return state
        state.collected_context["account"] = account_result.data["account"]
        policy_result = self._invoke(
            state,
            "check_compensation_policy",
            {
                "ticket_id": ticket["ticket_id"],
                "amount": state.parsed_intent.requested_credit if state.parsed_intent else 75.0,
            },
        )
        if not policy_result:
            return state
        state.collected_context["policy"] = policy_result.data
        self._mark(state, "Adapt", "completed", policy_result.data["explanation"])
        if not policy_result.data["allowed"]:
            escalation = self._invoke(
                state,
                "escalate_ticket",
                {
                    "ticket_id": ticket["ticket_id"],
                    "reason": (
                        f"Policy {policy_result.data['rule_id']} does not allow an automated "
                        f"${policy_result.data['amount']:.2f} credit. Specialist review required."
                    ),
                },
            )
            if escalation:
                state.evidence.append(f"escalation:{escalation.data['escalation_id']}")
                self._finish_needs_human(
                    state,
                    "Policy did not allow an automatic credit. The ticket was escalated; no account change was made.",
                )
            return state
        operation_key = f"supportops:{state.run_id}:{ticket['ticket_id']}"
        proposal = self._invoke(
            state,
            "propose_credit",
            {
                "ticket_id": ticket["ticket_id"],
                "amount": policy_result.data["amount"],
                "reason": f"Billing resolution for {ticket['ticket_id']}",
                "operation_key": operation_key,
            },
        )
        if not proposal:
            return state
        state.collected_context["proposal"] = proposal.data
        pending = proposal.data["approval_required"]
        if pending:
            account = state.collected_context["account"]
            state.pending_approval = PendingApproval(
                proposal_id=proposal.data["proposal_id"],
                ticket_id=ticket["ticket_id"],
                customer_id=account["customer_id"],
                customer_name=account["customer_name"],
                amount=proposal.data["amount"],
                reason=proposal.data["reason"],
                policy_decision=policy_result.data["rule"],
                risk_explanation=policy_result.data["explanation"],
                operation_key=operation_key,
            )
            StateMachine.transition(state, RunStatus.WAITING_FOR_APPROVAL)
            self._mark(state, "Execute", "completed", "Proposal created; account not changed.")
            self._mark(state, "Approve", "in_progress", "Waiting for explicit human approval.")
            audit_id = self.database.audit(
                state.run_id,
                "approval_requested",
                "agent",
                {
                    "proposal_id": proposal.data["proposal_id"],
                    "ticket_id": ticket["ticket_id"],
                    "amount": proposal.data["amount"],
                    "customer_id": account["customer_id"],
                },
            )
            state.evidence.append(f"audit:{audit_id}")
            self._save(state)
            return state
        self._mark(state, "Approve", "completed", "Policy did not require human approval.")
        return self._apply_and_verify(state)

    def _apply_and_verify(self, state: RunState) -> RunState:
        proposal = state.collected_context.get("proposal", {})
        apply_result = self._invoke(
            state,
            "apply_credit",
            {
                "proposal_id": proposal.get("proposal_id", ""),
                "operation_key": proposal.get("operation_key", ""),
            },
        )
        if not apply_result:
            return state
        credit = apply_result.data
        state.collected_context["credit"] = credit
        if state.status == RunStatus.EXECUTING:
            StateMachine.transition(state, RunStatus.VERIFYING)
        self._mark(state, "Execute", "completed", "Credit write returned from the simulated CRM.")
        self._mark(state, "Verify", "in_progress", "Checking the account independently.")
        verify = self._invoke(
            state,
            "verify_credit",
            {
                "customer_id": credit["customer_id"],
                "amount": credit["amount"],
                "operation_key": credit["operation_key"],
            },
        )
        if not verify:
            return state
        state.verification_result = verify.data
        if not verify.data["verified"]:
            self._mark(state, "Verify", "failed", "The account read did not match the expected credit.")
            self._fail(state, "Verification failed; the run will not report success.")
            return state
        self._mark(state, "Verify", "completed", "Independent account read confirmed the exact credit.")
        state.evidence.extend(
            [
                f"ticket:{state.collected_context['ticket']['ticket_id']}",
                f"credit:{credit['credit_id']}",
            ]
        )
        audit = self._invoke(state, "get_audit_log", {"run_id": state.run_id})
        if not audit:
            return state
        state.evidence.extend(f"audit:{entry['audit_id']}" for entry in audit.data["entries"])
        StateMachine.transition(state, RunStatus.COMPLETED)
        self._mark(state, "Complete", "completed", "Verified account update and evidence collected.")
        state.evidence = list(dict.fromkeys(state.evidence))
        state.final_summary = (
            f"Applied a ${credit['amount']:.2f} credit to {state.pending_approval.customer_name if state.pending_approval else state.collected_context['account']['customer_name']} "
            f"for ticket {state.collected_context['ticket']['ticket_id']}. Independent verification passed."
        )
        completion_audit = self.database.audit(
            state.run_id,
            "run_completed",
            "agent",
            {
                "credit_id": credit["credit_id"],
                "verified": True,
                "evidence_count": len(state.evidence),
            },
        )
        state.evidence.append(f"audit:{completion_audit}")
        self._save(state)
        log_event("run_completed", run_id=state.run_id, verified=True)
        return state

    def _finish_needs_human(self, state: RunState, summary: str) -> None:
        audit = self._invoke(state, "get_audit_log", {"run_id": state.run_id})
        if state.status in {RunStatus.EXECUTING, RunStatus.WAITING_FOR_APPROVAL}:
            StateMachine.transition(state, RunStatus.NEEDS_HUMAN_INPUT)
        if audit:
            state.evidence.extend(f"audit:{entry['audit_id']}" for entry in audit.data["entries"])
        state.final_summary = summary
        self._mark(state, "Complete", "blocked", "Human or specialist follow-up is required.")
        event_id = self.database.audit(
            state.run_id,
            "run_needs_human_input",
            "agent",
            {"summary": summary, "credit_applied": False},
        )
        state.evidence.append(f"audit:{event_id}")
        self._save(state)
        log_event("run_needs_human_input", run_id=state.run_id)

    def _invoke(
        self,
        state: RunState,
        tool_name: str,
        payload: dict[str, Any],
    ) -> ToolResult | None:
        safe_payload = sanitize(payload)
        self._mark(state, "Observe", "in_progress", f"Calling {tool_name}.")
        max_attempts = self.settings.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            record = ToolCallRecord(
                tool_name=tool_name,
                input=safe_payload,
                status="running",
                attempt=attempt,
            )
            state.tool_calls.append(record)
            self._save(state)
            started = time.monotonic()
            try:
                if tool_name in _FAILURE_TARGETS and state.simulate_permanent_failure:
                    state.injected_failure_seen = True
                    raise ToolExecutionError(
                        "Fictional CRM timeout: permanent failure scenario.",
                        code="simulated_crm_timeout",
                        retryable=True,
                    )
                if (
                    tool_name in _FAILURE_TARGETS
                    and state.simulate_transient_failure
                    and not state.injected_failure_seen
                ):
                    state.injected_failure_seen = True
                    self._save(state)
                    raise ToolExecutionError(
                        "Fictional CRM timeout: first request failed.",
                        code="simulated_crm_timeout",
                        retryable=True,
                    )
                result = self.registry.execute(tool_name, payload, state)
                record.status = "succeeded"
                record.completed_at = datetime.now(timezone.utc)
                observation = ToolObservation(
                    tool_name=tool_name,
                    success=True,
                    data=result.data,
                    message=result.message,
                )
                state.tool_observations.append(observation)
                audit_id = self.database.audit(
                    state.run_id,
                    "tool_succeeded",
                    "tool",
                    {
                        "tool_name": tool_name,
                        "attempt": attempt,
                        "input": safe_payload,
                        "observation": result.data,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    },
                )
                state.evidence.append(f"audit:{audit_id}")
                self._mark(state, "Observe", "completed", result.message)
                self._save(state)
                return result
            except ToolExecutionError as exc:
                record.status = "failed"
                record.error = redact_text(str(exc))
                record.completed_at = datetime.now(timezone.utc)
                state.tool_observations.append(
                    ToolObservation(tool_name=tool_name, success=False, message=record.error)
                )
                audit_id = self.database.audit(
                    state.run_id,
                    "tool_failed",
                    "tool",
                    {
                        "tool_name": tool_name,
                        "attempt": attempt,
                        "error_code": exc.code,
                        "message": record.error,
                    },
                )
                state.evidence.append(f"audit:{audit_id}")
                if exc.retryable and attempt < max_attempts:
                    state.retry_counts[tool_name] = state.retry_counts.get(tool_name, 0) + 1
                    self._mark(
                        state,
                        "Adapt",
                        "in_progress",
                        f"{tool_name} timed out; retry {state.retry_counts[tool_name]} of {self.settings.max_retries}.",
                    )
                    self._save(state)
                    time.sleep(self.settings.retry_base_delay_seconds * (2 ** (attempt - 1)))
                    continue
                self._fail(state, f"{tool_name} failed safely: {record.error}")
                return None
            except sqlite3.Error:
                record.status = "failed"
                record.error = f"{tool_name} could not complete because the local database was unavailable."
                record.completed_at = datetime.now(timezone.utc)
                self.database.audit(
                    state.run_id,
                    "tool_failed",
                    "tool",
                    {"tool_name": tool_name, "attempt": attempt, "error_code": "database_error"},
                )
                self._fail(state, record.error)
                return None
            except Exception:
                record.status = "failed"
                record.error = f"{tool_name} returned an unexpected internal error."
                record.completed_at = datetime.now(timezone.utc)
                self.database.audit(
                    state.run_id,
                    "tool_failed",
                    "tool",
                    {"tool_name": tool_name, "attempt": attempt, "error_code": "internal_error"},
                )
                self._fail(state, record.error)
                return None
        return None

    def _fail(self, state: RunState, message: str) -> None:
        safe_message = redact_text(message)
        if state.status not in {RunStatus.FAILED, RunStatus.COMPLETED, RunStatus.NEEDS_HUMAN_INPUT}:
            StateMachine.transition(state, RunStatus.FAILED)
        state.error_details = safe_message
        state.final_summary = safe_message
        self._mark(state, "Complete", "failed", safe_message)
        self._save(state)
        log_event("run_failed", run_id=state.run_id, error_code="safe_failure")

    def _mark(self, state: RunState, name: str, status: str, details: str = "") -> None:
        item = next((event for event in state.timeline if event.name == name), None)
        if item:
            item.status = status
            item.details = redact_text(details)
            item.updated_at = datetime.now(timezone.utc)
        if status == "in_progress" or name == "Complete":
            state.current_step = name
        self._save(state)

    def _save(self, state: RunState) -> None:
        self.database.save_run(state)