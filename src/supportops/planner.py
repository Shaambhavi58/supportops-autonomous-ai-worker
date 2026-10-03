"""Deterministic and optional OpenAI-compatible typed plan generation."""

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from supportops.config import Settings
from supportops.errors import LLMPlanningError
from supportops.logging_utils import redact_text
from supportops.models import ParsedIntent, PlanStep

CANONICAL_TOOLS = [
    "list_tickets",
    "get_ticket",
    "get_customer_account",
    "check_compensation_policy",
    "propose_credit",
    "apply_credit",
    "verify_credit",
    "get_audit_log",
]

_PLAN_TITLES = {
    "list_tickets": ("Find unresolved billing work", "Compare open billing tickets by priority."),
    "get_ticket": ("Inspect the selected ticket", "Read the issue details before taking action."),
    "get_customer_account": ("Inspect the customer account", "Confirm the account and existing credits."),
    "check_compensation_policy": ("Check company policy", "Evaluate eligibility and approval requirements."),
    "propose_credit": ("Prepare a credit proposal", "Record the proposed action without changing the account."),
    "apply_credit": ("Apply an approved credit", "Only proceed after any required approval is recorded."),
    "verify_credit": ("Verify the account update", "Read the account independently after the write."),
    "get_audit_log": ("Collect evidence", "Retrieve the run's auditable evidence trail."),
}


def parse_intent(goal: str) -> ParsedIntent:
    redacted = redact_text(goal)
    amount_match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", redacted)
    if not amount_match:
        amount_match = re.search(
            r"\b(?:credit|refund|compensat\w*)\b(?:\s+(?:of|for|amount))?\s+(\d+(?:\.\d{1,2})?)",
            redacted,
            re.IGNORECASE,
        )
    amount = float(amount_match.group(1)) if amount_match else 75.0
    return ParsedIntent(
        objective=redacted,
        requested_credit=amount,
    )


class _LLMStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=2, max_length=100)
    tool_name: str = Field(min_length=2, max_length=60)
    rationale: str = Field(min_length=2, max_length=300)


class _LLMPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[_LLMStep] = Field(min_length=8, max_length=12)


class Planner:
    def __init__(self, settings: Settings, allowed_tools: set[str]):
        self.settings = settings
        self.allowed_tools = allowed_tools

    def plan(self, goal: str, intent: ParsedIntent) -> list[PlanStep]:
        if self.settings.demo_mode:
            return self._deterministic_plan()
        return self._llm_plan(goal, intent)

    def _deterministic_plan(self) -> list[PlanStep]:
        return [
            PlanStep(title=_PLAN_TITLES[name][0], tool_name=name, rationale=_PLAN_TITLES[name][1])
            for name in CANONICAL_TOOLS
        ]

    def _llm_plan(self, goal: str, intent: ParsedIntent) -> list[PlanStep]:
        if not self.settings.llm_api_key:
            raise LLMPlanningError("LLM_API_KEY is required when DEMO_MODE=false.")
        system = (
            "Create a concise JSON plan with a 'steps' array. Each step has title, tool_name, rationale. "
            "Only use these registered tool names: "
            + ", ".join(sorted(self.allowed_tools))
            + ". Include the mandatory safe workflow in order: "
            + ", ".join(CANONICAL_TOOLS)
            + ". Do not include arguments, URLs, code, shell commands, or additional tools. "
            "Applying a credit always occurs only after a recorded human approval when policy requires it."
        )
        user = json.dumps(
            {"goal": redact_text(goal), "parsed_intent": intent.model_dump(mode="json")},
            ensure_ascii=False,
        )
        try:
            with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
                response = client.post(
                    f"{self.settings.llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                    json={
                        "model": self.settings.llm_model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = _LLMPlan.model_validate_json(content)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise LLMPlanningError(
                "The optional planner could not return a valid structured plan. No action was taken."
            ) from exc
        names = [step.tool_name for step in parsed.steps]
        if any(name not in self.allowed_tools for name in names):
            raise LLMPlanningError("The planner requested an unregistered tool. No action was taken.")
        if names != CANONICAL_TOOLS:
            raise LLMPlanningError(
                "The planner changed the mandatory safe workflow. No action was taken."
            )
        return [
            PlanStep(title=step.title, tool_name=step.tool_name, rationale=step.rationale)
            for step in parsed.steps
        ]