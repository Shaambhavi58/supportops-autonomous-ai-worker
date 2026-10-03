# Limitations

- This build intentionally uses a local fictional support environment. It does not connect to Freshdesk, payment processors, production customer records, or a real CRM.
- Approval is represented by a decision in the Streamlit application; production use would require authenticated reviewer identities, authorization, and separation of duties.
- The optional LLM creates a structured plan only. It does not decide policy outcomes, access external tools, or write account state. Its output is validated and the same deterministic safety sequence executes afterward.
- The amount parser supports explicit dollar amounts and defaults to $75 when no amount is present; it is not a general business-language parser.
- SQLite is appropriate for this single-process demo. Multi-instance production use should move run and approval state to a shared transactional database and add concurrency and retention policies.
- The local audit table is tamper-evident only by process boundaries, not cryptographically signed or immutable.
- No real customer PII is included in the fixtures. A production adapter would need privacy, retention, authorization, and incident-response controls before handling customer data.
- The app does not send email or notify a specialist when an escalation is created; escalation is visible in its state and audit record.