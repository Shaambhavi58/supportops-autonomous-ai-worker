"""SQLite persistence for simulated company data, runs, approvals, and audit records."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from supportops.logging_utils import sanitize
from supportops.models import RunState


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(sanitize(value), default=str, sort_keys=True)


class Database:
    def __init__(self, path: Path, approval_threshold: float = 50.0):
        self.path = Path(path)
        self.approval_threshold = approval_threshold
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY, subject TEXT NOT NULL, description TEXT NOT NULL,
                    priority TEXT NOT NULL, status TEXT NOT NULL, category TEXT NOT NULL,
                    customer_id TEXT NOT NULL, tags_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_accounts (
                    customer_id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, plan TEXT NOT NULL,
                    balance REAL NOT NULL CHECK(balance >= 0)
                );
                CREATE TABLE IF NOT EXISTS credits (
                    credit_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customer_accounts(customer_id),
                    ticket_id TEXT, amount REAL NOT NULL CHECK(amount > 0), reason TEXT NOT NULL,
                    operation_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL,
                    operation_key TEXT NOT NULL UNIQUE, approval_required INTEGER NOT NULL,
                    policy_rule TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
                    decision_reason TEXT, created_at TEXT NOT NULL, decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS escalations (
                    escalation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
                    reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_filters ON tickets(status, priority, category);
                CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_logs(run_id, audit_id);
                """
            )
            credit_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(credits)").fetchall()
            }
            if "ticket_id" not in credit_columns:
                connection.execute("ALTER TABLE credits ADD COLUMN ticket_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_credit_per_ticket "
                "ON credits(ticket_id) WHERE ticket_id IS NOT NULL"
            )
            count = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        if not count:
            self.seed_demo()

    def seed_demo(self) -> None:
        fixtures = Path(__file__).resolve().parents[2] / "fixtures"
        tickets = json.loads((fixtures / "tickets.json").read_text(encoding="utf-8"))
        accounts = json.loads((fixtures / "accounts.json").read_text(encoding="utf-8"))
        with self.connect() as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO customer_accounts(customer_id,customer_name,plan,balance)
                   VALUES(:customer_id,:customer_name,:plan,:balance)""",
                accounts,
            )
            connection.executemany(
                """INSERT OR IGNORE INTO tickets
                   (ticket_id,subject,description,priority,status,category,customer_id,tags_json)
                   VALUES(:ticket_id,:subject,:description,:priority,:status,:category,:customer_id,:tags)""",
                [{**ticket, "tags": json.dumps(ticket["tags"])} for ticket in tickets],
            )
            for account in accounts:
                for index, credit in enumerate(account["previous_credits"], start=1):
                    operation_key = f"seed:{account['customer_id']}:{index}"
                    connection.execute(
                        """INSERT OR IGNORE INTO credits
                           (credit_id,customer_id,amount,reason,operation_key,run_id,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            f"CR-SEED-{account['customer_id']}-{index}",
                            account["customer_id"],
                            credit["amount"],
                            credit["reason"],
                            operation_key,
                            "demo-seed",
                            _now(),
                        ),
                    )

    def reset(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            for table in (
                "audit_logs", "runs", "approvals", "proposals", "escalations", "credits",
                "tickets", "customer_accounts",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM sqlite_sequence WHERE name='audit_logs'")
        self.seed_demo()

    def save_run(self, state: RunState) -> None:
        state.updated_at = datetime.now(timezone.utc)
        payload = state.model_dump(mode="json")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runs(run_id,state_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET state_json=excluded.state_json,
                   updated_at=excluded.updated_at""",
                (state.run_id, _json(payload), _now()),
            )

    def get_run(self, run_id: str) -> RunState | None:
        with self.connect() as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunState.model_validate_json(row["state_json"]) if row else None

    def list_runs(self, limit: int = 30) -> list[RunState]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunState.model_validate_json(row["state_json"]) for row in rows]

    def list_tickets(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if priority:
            clauses.append("priority=?")
            values.append(priority)
        if category:
            clauses.append("LOWER(category)=LOWER(?)")
            values.append(category)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sort = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
        offset = (page - 1) * page_size
        with self.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM tickets{where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM tickets{where} ORDER BY {sort},ticket_id LIMIT ? OFFSET ?",
                [*values, page_size, offset],
            ).fetchall()
        return [self._ticket(row) for row in rows], total

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        return self._ticket(row) if row else None

    def search_tickets(self, query: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        term = f"%{query.casefold()}%"
        predicate = "(LOWER(subject) LIKE ? OR LOWER(description) LIKE ? OR LOWER(tags_json) LIKE ?)"
        values = [term, term, term]
        with self.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM tickets WHERE {predicate}", values).fetchone()[0]
            rows = connection.execute(
                f"""SELECT * FROM tickets WHERE {predicate}
                    ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END,ticket_id LIMIT ? OFFSET ?""",
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
        return [self._ticket(row) for row in rows], total

    @staticmethod
    def _ticket(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        return result

    def get_customer_for_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.* FROM customer_accounts a JOIN tickets t ON t.customer_id=a.customer_id
                   WHERE t.ticket_id=?""",
                (ticket_id,),
            ).fetchone()
            if not row:
                return None
            account = dict(row)
            credits = connection.execute(
                """SELECT credit_id,amount,reason,created_at FROM credits
                   WHERE customer_id=? ORDER BY created_at DESC""",
                (account["customer_id"],),
            ).fetchall()
        account["previous_credits"] = [dict(item) for item in credits]
        return account

    def policy_document(self) -> dict[str, Any]:
        fixture = Path(__file__).resolve().parents[2] / "fixtures" / "policy.json"
        return json.loads(fixture.read_text(encoding="utf-8"))

    def create_proposal(self, proposal: dict[str, Any]) -> None:
        with self.connect() as connection:
            existing_credit = connection.execute(
                "SELECT credit_id FROM credits WHERE ticket_id=?",
                (proposal["ticket_id"],),
            ).fetchone()
            if existing_credit:
                raise ValueError("A credit has already been applied to this ticket.")
            connection.execute(
                """INSERT INTO proposals
                   (proposal_id,run_id,ticket_id,customer_id,amount,reason,operation_key,
                    approval_required,policy_rule,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    proposal["proposal_id"], proposal["run_id"], proposal["ticket_id"],
                    proposal["customer_id"], proposal["amount"], proposal["reason"],
                    proposal["operation_key"], int(proposal["approval_required"]),
                    proposal["policy_rule"], _now(),
                ),
            )
            if proposal["approval_required"]:
                connection.execute(
                    """INSERT INTO approvals
                       (approval_id,run_id,proposal_id,status,created_at) VALUES(?,?,?,'pending',?)""",
                    (f"APR-{proposal['proposal_id']}", proposal["run_id"], proposal["proposal_id"], _now()),
                )

    def record_approval(self, run_id: str, proposal_id: str, approved: bool, reason: str = "") -> str:
        status = "approved" if approved else "rejected"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM approvals WHERE run_id=? AND proposal_id=?",
                (run_id, proposal_id),
            ).fetchone()
            if not row:
                connection.rollback()
                raise ValueError("No approval request exists for this proposal.")
            if row["status"] != "pending":
                connection.rollback()
                raise ValueError("This approval request already has a decision.")
            connection.execute(
                """UPDATE approvals SET status=?,decision_reason=?,decided_at=?
                   WHERE run_id=? AND proposal_id=? AND status='pending'""",
                (status, reason, _now(), run_id, proposal_id),
            )
            approval_id = f"APR-{proposal_id}"
            connection.commit()
        return approval_id

    def apply_proposal(self, proposal_id: str, operation_key: str, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM credits WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if existing:
                connection.commit()
                return {**dict(existing), "applied": False, "idempotent_replay": True}
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=? AND operation_key=?",
                (proposal_id, operation_key),
            ).fetchone()
            if not proposal or proposal["run_id"] != run_id:
                connection.rollback()
                raise ValueError("Credit proposal is missing or does not belong to this run.")
            approval_required = bool(proposal["approval_required"]) or proposal["amount"] > self.approval_threshold
            if approval_required:
                approval = connection.execute(
                    "SELECT status FROM approvals WHERE run_id=? AND proposal_id=?",
                    (run_id, proposal_id),
                ).fetchone()
                if not approval or approval["status"] != "approved":
                    connection.rollback()
                    raise PermissionError("An approved human decision is required before this credit can be applied.")
            credit_id = f"CR-{proposal_id}"
            connection.execute(
                """INSERT INTO credits
                   (credit_id,customer_id,ticket_id,amount,reason,operation_key,run_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    credit_id, proposal["customer_id"], proposal["ticket_id"],
                    proposal["amount"], proposal["reason"],
                    operation_key, run_id, _now(),
                ),
            )
            connection.execute(
                "UPDATE customer_accounts SET balance=balance+? WHERE customer_id=?",
                (proposal["amount"], proposal["customer_id"]),
            )
            result = connection.execute(
                "SELECT * FROM credits WHERE credit_id=?", (credit_id,)
            ).fetchone()
            connection.commit()
        return {**dict(result), "applied": True, "idempotent_replay": False}

    def verify_credit(self, customer_id: str, amount: float, operation_key: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM credits WHERE customer_id=? AND operation_key=?",
                (customer_id, operation_key),
            ).fetchone()
            account = connection.execute(
                "SELECT balance FROM customer_accounts WHERE customer_id=?", (customer_id,)
            ).fetchone()
        matched = bool(row and abs(float(row["amount"]) - amount) < 0.005 and account)
        return {
            "verified": matched,
            "credit": dict(row) if row else None,
            "current_balance": float(account["balance"]) if account else None,
        }

    def create_escalation(self, run_id: str, ticket_id: str, reason: str) -> str:
        escalation_id = f"ESC-{run_id[:8].upper()}"
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO escalations
                   (escalation_id,run_id,ticket_id,reason,status,created_at)
                   VALUES(?,?,?,?,'open',?)""",
                (escalation_id, run_id, ticket_id, reason, _now()),
            )
        return escalation_id

    def audit(self, run_id: str, event_type: str, actor: str, payload: dict[str, Any]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO audit_logs(run_id,event_type,actor,payload_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (run_id, event_type, actor, _json(payload), _now()),
            )
            return int(cursor.lastrowid)

    def get_audit(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_logs WHERE run_id=? ORDER BY audit_id", (run_id,)
            ).fetchall()
        return [
            {
                "audit_id": row["audit_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]