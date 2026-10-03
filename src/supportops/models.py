"""Typed state, tool contracts, and persisted agent records."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunStatus(StrEnum):
    PLANNING = "planning"
    EXECUTING = "executing"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN_INPUT = "needs_human_input"


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PlanStep(StrictModel):
    title: str
    tool_name: str
    rationale: str


class ParsedIntent(StrictModel):
    objective: str
    target: str = "highest-priority unresolved billing ticket"
    requested_credit: float = Field(gt=0, le=10000)


class ToolCallRecord(StrictModel):
    tool_name: str
    input: dict[str, Any]
    status: str
    attempt: int
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error: str | None = None


class ToolObservation(StrictModel):
    tool_name: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class PendingApproval(StrictModel):
    proposal_id: str
    ticket_id: str
    customer_id: str
    customer_name: str
    amount: float
    reason: str
    policy_decision: str
    risk_explanation: str
    operation_key: str


class TimelineEvent(StrictModel):
    name: str
    status: str
    details: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class RunState(StrictModel):
    run_id: str
    original_goal: str
    parsed_intent: ParsedIntent | None = None
    status: RunStatus = RunStatus.PLANNING
    generated_plan: list[PlanStep] = Field(default_factory=list)
    current_step: str = "Understand"
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_observations: list[ToolObservation] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    collected_context: dict[str, Any] = Field(default_factory=dict)
    pending_approval: PendingApproval | None = None
    expected_outcome: str = ""
    verification_result: dict[str, Any] = Field(default_factory=dict)
    final_summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    error_details: str | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    simulate_transient_failure: bool = False
    simulate_permanent_failure: bool = False
    injected_failure_seen: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ToolResult(StrictModel):
    tool_name: str
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "OK"


class ListTicketsInput(StrictModel):
    status: TicketStatus | None = None
    priority: Priority | None = None
    category: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class GetTicketInput(StrictModel):
    ticket_id: str = Field(min_length=1)


class SearchTicketsInput(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class GetCustomerAccountInput(StrictModel):
    ticket_id: str = Field(min_length=1)


class PolicyInput(StrictModel):
    ticket_id: str = Field(min_length=1)
    amount: float = Field(gt=0, le=10000)


class ProposeCreditInput(StrictModel):
    ticket_id: str = Field(min_length=1)
    amount: float = Field(gt=0, le=10000)
    reason: str = Field(min_length=3, max_length=500)
    operation_key: str = Field(min_length=8, max_length=200)


class ApplyCreditInput(StrictModel):
    proposal_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=8, max_length=200)


class VerifyCreditInput(StrictModel):
    customer_id: str = Field(min_length=1)
    amount: float = Field(gt=0, le=10000)
    operation_key: str = Field(min_length=8, max_length=200)


class EscalateTicketInput(StrictModel):
    ticket_id: str = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=1000)


class GetAuditLogInput(StrictModel):
    run_id: str = Field(min_length=1)