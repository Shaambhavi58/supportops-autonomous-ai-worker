"""Streamlit interface for SupportOps AI Worker."""

from typing import Any

import streamlit as st

from supportops.agent import SupportOpsAgent
from supportops.config import Settings, get_settings
from supportops.database import Database
from supportops.models import RunState, RunStatus

EXAMPLES = {
    "Successful billing resolution ($75; approval required)": (
        "Find the highest-priority unresolved billing ticket, inspect the customer's account, "
        "check whether a $75 credit is allowed under policy, request approval if required, "
        "apply the approved credit, verify the account update, and return evidence."
    ),
    "Policy denial ($150; escalation)": (
        "Find the highest-priority unresolved billing ticket and determine whether a $150 "
        "credit is allowed. Escalate it if policy does not allow an automatic credit."
    ),
    "Small eligible credit ($25; no approval)": (
        "Find the highest-priority unresolved billing ticket and apply a policy-eligible "
        "$25 credit if permitted, then verify the account update."
    ),
}

STATUS_LABELS = {
    RunStatus.PLANNING: "Planning",
    RunStatus.EXECUTING: "Executing",
    RunStatus.WAITING_FOR_APPROVAL: "Waiting for approval",
    RunStatus.VERIFYING: "Verifying",
    RunStatus.COMPLETED: "Completed",
    RunStatus.FAILED: "Failed",
    RunStatus.NEEDS_HUMAN_INPUT: "Needs human input",
}


@st.cache_resource
def get_database(path: str, threshold: float) -> Database:
    return Database(path, approval_threshold=threshold)


def _state_json(state: RunState) -> dict[str, Any]:
    return state.model_dump(mode="json")


def _render_timeline(state: RunState) -> None:
    st.subheader("Execution timeline")
    events = {event.name: event for event in state.timeline}
    names = ("Understand", "Plan", "Execute", "Observe", "Adapt", "Approve", "Verify", "Complete")
    for offset in range(0, len(names), 4):
        columns = st.columns(4)
        for column, name in zip(columns, names[offset : offset + 4]):
            event = events.get(name)
            with column:
                if not event:
                    st.write(f"**{name}**")
                    st.caption("Not started")
                    continue
                st.write(f"**{name}**")
                st.caption(event.status.replace("_", " ").title())
                if event.details:
                    st.caption(event.details)


def _render_approval(agent: SupportOpsAgent, state: RunState) -> None:
    pending = state.pending_approval
    if not pending:
        return
    st.warning("A human decision is required before the account can be changed.")
    st.subheader("Approval request")
    left, right = st.columns(2)
    left.write(f"**Proposed action:** Apply a ${pending.amount:.2f} account credit")
    left.write(f"**Customer:** {pending.customer_name} ({pending.customer_id})")
    left.write(f"**Ticket:** {pending.ticket_id}")
    left.write(f"**Reason:** {pending.reason}")
    right.write(f"**Policy decision:** {pending.policy_decision}")
    right.write(f"**Risk explanation:** {pending.risk_explanation}")
    decision_reason = st.text_input(
        "Decision note (optional)",
        key=f"decision_note_{state.run_id}",
        placeholder="Add context for the audit trail",
    )
    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve", type="primary", key=f"approve_{state.run_id}"):
        try:
            agent.resume_approval(state.run_id, approved=True, reason=decision_reason)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if reject_col.button("Reject", key=f"reject_{state.run_id}"):
        try:
            agent.resume_approval(state.run_id, approved=False, reason=decision_reason)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _render_run(state: RunState, agent: SupportOpsAgent, database: Database) -> None:
    st.divider()
    st.header("Current run")
    st.caption(f"Run ID: `{state.run_id}`")
    status_col, step_col = st.columns(2)
    status_col.metric("Status", STATUS_LABELS[state.status])
    step_col.metric("Current step", state.current_step)
    _render_timeline(state)
    _render_approval(agent, state)

    with st.expander("Generated plan", expanded=True):
        if state.generated_plan:
            st.dataframe(
                [
                    {
                        "Step": index,
                        "Action": item.title,
                        "Whitelisted tool": item.tool_name,
                        "Why": item.rationale,
                    }
                    for index, item in enumerate(state.generated_plan, start=1)
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("No plan was generated.")

    with st.expander("Tool calls, observations, and retries", expanded=True):
        if not state.tool_calls:
            st.info("No tools have run yet.")
        for index, call in enumerate(state.tool_calls, start=1):
            title = f"{index}. {call.tool_name} — {call.status} (attempt {call.attempt})"
            with st.expander(title, expanded=call.status == "failed"):
                st.write("Input")
                st.json(call.input)
                observation = next(
                    (
                        item
                        for item in reversed(state.tool_observations)
                        if item.tool_name == call.tool_name
                        and item.created_at >= call.started_at
                    ),
                    None,
                )
                if observation:
                    st.write("Observation")
                    st.json(observation.data if observation.success else observation.message)
                if call.error:
                    st.error(call.error)
        if state.retry_counts:
            st.write("**Retries**")
            st.json(state.retry_counts)

    if state.final_summary:
        if state.status == RunStatus.COMPLETED:
            st.success(state.final_summary)
        elif state.status == RunStatus.FAILED:
            st.error(state.final_summary)
        else:
            st.info(state.final_summary)
    if state.verification_result:
        st.subheader("Verification")
        st.write(
            "Passed" if state.verification_result.get("verified") else "Not verified",
        )
        st.json(state.verification_result)
    if state.evidence:
        st.subheader("Evidence")
        st.write(", ".join(f"`{item}`" for item in dict.fromkeys(state.evidence)))

    with st.expander("Full audit log"):
        st.json(database.get_audit(state.run_id))


def main() -> None:
    st.set_page_config(page_title="SupportOps AI Worker", page_icon=None, layout="wide")
    st.title("SupportOps AI Worker")
    st.write(
        "A transparent support-operations agent that works against fictional company data, "
        "asks before risky credits, and independently verifies every account change."
    )
    try:
        settings = get_settings()
        database = get_database(str(settings.db_path), settings.approval_threshold)
        agent = SupportOpsAgent(settings=settings, database=database)
    except Exception as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()

    mode = "Deterministic offline demo" if settings.demo_mode else f"Optional LLM planner: {settings.llm_model}"
    st.caption(
        f"Mode: {mode} · Approval threshold: ${settings.approval_threshold:.2f} · "
        "All customer and ticket data is fictional."
    )
    with st.sidebar:
        st.header("Demo controls")
        failure_mode = st.radio(
            "CRM scenario",
            ("Normal", "Simulate transient CRM failure", "Permanent CRM failure"),
            help="Transient mode times out once and recovers. Permanent mode exhausts bounded retries safely.",
        )
        st.caption(f"Maximum attempts per retryable tool: {settings.max_retries + 1}")
        st.divider()
        st.subheader("Reset simulated data")
        st.warning("Reset clears run history and all credits applied in this local demo database.")
        confirm_reset = st.checkbox("I understand this clears demo state")
        if st.button("Reset demo", disabled=not confirm_reset):
            database.reset()
            st.session_state.pop("current_run_id", None)
            st.success("Demo data reset.")
            st.rerun()

    example = st.selectbox("Example task", ["Custom task", *EXAMPLES.keys()])
    with st.form("run_task_form"):
        default_goal = EXAMPLES.get(example, "")
        goal = st.text_area(
            "Task objective",
            value=default_goal,
            height=130,
            max_chars=2000,
            placeholder="Describe the outcome you want the support worker to achieve.",
            key=f"goal_{example}",
        )
        run_submitted = st.form_submit_button("Run task", type="primary")

    if run_submitted:
        try:
            result = agent.run(
                goal,
                simulate_transient_failure=failure_mode == "Simulate transient CRM failure",
                simulate_permanent_failure=failure_mode == "Permanent CRM failure",
            )
            st.session_state["current_run_id"] = result.run_id
        except Exception as exc:
            st.error(str(exc))

    runs = database.list_runs()
    current_id = st.session_state.get("current_run_id")
    if runs:
        labels = {
            run.run_id: f"{STATUS_LABELS[run.status]} · {run.created_at:%Y-%m-%d %H:%M:%S} · {run.run_id[:8]}"
            for run in runs
        }
        selected_index = next(
            (index for index, run in enumerate(runs) if run.run_id == current_id),
            0,
        )
        selected_run = st.selectbox(
            "Saved runs",
            options=[run.run_id for run in runs],
            index=selected_index,
            format_func=lambda run_id: labels[run_id],
        )
        state = database.get_run(selected_run)
        if state:
            st.session_state["current_run_id"] = state.run_id
            _render_run(state, agent, database)
    else:
        st.info("Run the example task to see the agent plan, approvals, and evidence.")


if __name__ == "__main__":
    main()