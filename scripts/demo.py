"""Run a complete, offline approval-and-verification example."""

import json
from pathlib import Path

from supportops.agent import SupportOpsAgent
from supportops.config import Settings
from supportops.database import Database
from supportops.models import RunStatus


def main() -> int:
    settings = Settings(
        demo_mode=True,
        db_path=Path("data/demo.sqlite3"),
        approval_threshold=50,
        max_retries=2,
        retry_base_delay_seconds=0.01,
    )
    database = Database(settings.db_path, approval_threshold=settings.approval_threshold)
    database.reset()
    agent = SupportOpsAgent(settings=settings, database=database)
    run = agent.run(
        "Find the highest-priority unresolved billing ticket, inspect the customer account, "
        "check whether a $75 credit is allowed, request approval if required, apply the "
        "approved credit, verify the account update, and return evidence."
    )
    if run.status == RunStatus.WAITING_FOR_APPROVAL:
        pending = run.pending_approval
        print(
            f"Approval requested: ${pending.amount:.2f} for {pending.customer_name} "
            f"on ticket {pending.ticket_id}; approving for this scripted demo."
        )
        run = agent.resume_approval(run.run_id, approved=True, reason="Approved by scripted demo reviewer.")
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "status": run.status.value,
                "summary": run.final_summary,
                "verified": run.verification_result.get("verified", False),
                "evidence": run.evidence,
            },
            indent=2,
        )
    )
    return 0 if run.status == RunStatus.COMPLETED and run.verification_result.get("verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())