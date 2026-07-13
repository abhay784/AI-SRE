# SlopSaver — Autonomous Application-Layer Reliability Agent (Full v1 Build Plan)

## Context

Small online businesses run on third-party stacks (Stripe, S3, managed Postgres, SES/SendGrid, DNS registrars) with zero monitoring at the application/business-logic layer. Edge tools like Cloudflare can't see a silently failing Stripe webhook, an S3 policy flip, a pool-exhausted database, or a lapsed cert. The PRD defines an agent that **detects** these failures with cheap deterministic collectors, **reasons** about root cause/severity with an LLM (only when an anomaly fires), **remediates** automatically where safe (P3/P2) or escalates for approval (P1), and **reports** in plain English.

The repo (`/Users/abhaykorlapati/SlopSaver`) is empty — this is a greenfield build covering **all 9 PRD milestones**.

## Decisions (confirmed with user)

| Decision | Choice |
|---|---|
| Scope | Full v1 — all 9 milestones |
| Language | Python everywhere (orchestrator, adapters, sandbox apps) |
| Sandbox infra | **kind** (local Kubernetes) — realistic for the K8s-rollback remediation path |
| Reasoning layer | **Claude Agent SDK** (`claude-agent-sdk` Python package), model `claude-opus-4-8` |

Key architectural principle from the PRD, preserved throughout: **the LLM runs only when an anomaly fires**. Polling and thresholding are deterministic code. Remediation execution stays in deterministic code too — the agent *proposes* actions via a custom tool; the orchestrator executes P3/P2 and queues P1 for human approval.

## Repository layout

```
SlopSaver/
├── pyproject.toml                  # package: slopsaver; deps: fastapi, anthropic/claude-agent-sdk,
│                                   # stripe, boto3, psycopg[binary], httpx, pydantic, kubernetes, pyyaml
├── slopsaver/
│   ├── core/
│   │   ├── models.py               # HealthResult, AnomalyEvent, RemediationAction, Severity(P1/P2/P3), IncidentReport
│   │   ├── adapter.py              # BaseAdapter ABC: check_health() / get_context() / remediate(action, params)
│   │   ├── registry.py             # adapter discovery + per-adapter poll schedule from config
│   │   ├── scheduler.py            # asyncio loop: poll adapters on independent cadences
│   │   ├── rules.py                # deterministic anomaly rules/thresholds (no LLM)
│   │   ├── reasoning.py            # Claude Agent SDK invocation (context packet → decision)
│   │   ├── approvals.py            # P1 approval queue (propose → wait → execute/reject)
│   │   ├── audit.py                # append-only JSONL: LLM calls (tokens/latency/decision) + actions taken
│   │   └── report.py               # non-technical incident report generator
│   ├── adapters/
│   │   ├── stripe_adapter.py       # charge↔order reconciliation, webhook replay
│   │   ├── postgres_adapter.py     # pg_stat_activity pool monitoring, kill idle conns
│   │   ├── s3_adapter.py           # policy/ACL diff vs baseline, synthetic object fetch, revert
│   │   ├── ssl_adapter.py          # TLS handshake, notAfter
│   │   ├── dns_adapter.py          # registrar expiry + multi-resolver checks
│   │   ├── email_adapter.py        # SES/SendGrid bounce rate + quota, provider failover
│   │   ├── traffic_adapter.py      # per-endpoint/IP rate anomaly + form-spam signals
│   │   ├── deploy_adapter.py       # deploy-event correlation, K8s rollback
│   │   └── backup_adapter.py       # test-restore/checksum verification
│   └── selfmonitor/
│       ├── heartbeat.py            # dead-man's-switch ping (Healthchecks.io-style URL)
│       └── dashboard.py            # per-customer cost/incident stats from audit log
├── sandbox/
│   ├── kind-config.yaml            # kind cluster definition
│   ├── k8s/                        # manifests: api-gateway, order-service, auth-service,
│   │                               #   postgres (low max_connections), minio, mailpit
│   ├── apps/                       # FastAPI fake-restaurant services (orders, reservations, menu images)
│   └── chaos/                      # one script per failure mode (10 total), e.g.
│       ├── drop_stripe_webhook.py
│       ├── exhaust_db_pool.py
│       ├── flip_s3_policy.py
│       └── ...
├── tests/
│   ├── golden/                     # trigger → detect → decide → remediate → report, per failure mode
│   └── unit/                       # rules thresholds, adapter parsing, report formatting
└── config/
    └── customer.example.yaml       # integrations, credentials via env refs, poll cadences, severity policy
```

## Build order (follows PRD milestones)

### Milestone 1 — Sandbox + chaos injection (test harness first)
- `kind` cluster config + K8s manifests: three FastAPI services (api-gateway, order-service, auth-service), Postgres with `max_connections=20`, MinIO as S3 stand-in, Mailpit as SMTP sink.
- FastAPI fake-restaurant app: `/orders` (Stripe test-mode webhook receiver writing to Postgres), `/menu/{img}` (serves from MinIO), `/reserve` (form endpoint).
- Chaos scripts, one per PRD failure row — each is a small CLI (`python -m sandbox.chaos.flip_s3_policy`) so tests and demos have repeatable triggers. Stripe scenarios use Stripe **test mode** with a deliberately unprocessed event to simulate a missed webhook.

### Milestone 2 — Core skeleton + Stripe reconciliation adapter
- `core/models.py`, `core/adapter.py` (the PRD's pluggable interface), `core/scheduler.py`, `core/rules.py`, `core/audit.py`.
- `stripe_adapter.py`: list recent charges/events via restricted key, reconcile against orders table, detect charge-without-order; remediation = replay event / reconstruct order from charge object.
- **User contribution point:** the reconciliation matching rule (what counts as a "match" between a charge and an order — amount+metadata? time window?) is a business-logic judgment call; a marked TODO in `stripe_adapter.py` will define it.

### Milestone 3 — DB pool + S3 adapters
- `postgres_adapter.py`: poll `pg_stat_activity` vs `max_connections` (read-only role), alert ≥80%; remediation = terminate idle-in-transaction backends via a separate scoped role.
- `s3_adapter.py`: `head_object` synthetic fetch + `get_bucket_policy`/`get_bucket_acl` diffed against a stored last-known-good baseline; remediation = `put_bucket_policy` revert to baseline only. Public-flip is auto-classified P1 (security).

### Milestone 4 — Reasoning agent + severity tiers + approval flow
- `core/reasoning.py` built on the **Claude Agent SDK** (`claude-agent-sdk` pip package, docs: code.claude.com/docs/en/agent-sdk):
  - `ClaudeAgentOptions(model="claude-opus-4-8", allowed_tools=[...], permission_mode="default")`, system prompt describing the SRE role and P1/P2/P3 policy.
  - Custom tools exposed via `@tool` + `create_sdk_mcp_server`: `get_adapter_context(adapter)` (pulls `adapter.get_context()`) and `propose_remediation(action, params, severity, rationale)`. The agent never executes side effects itself — it proposes; deterministic code executes.
  - Invocation: anomaly fires → context packet (PRD §7 JSON shape) → `query()` → structured decision parsed from the `propose_remediation` call.
  - Per-run logging from the SDK's result message (`total_cost_usd`, usage tokens, duration) into `core/audit.py` — satisfies PRD §8 LLM-call logging.
- `core/approvals.py`: P3 auto-execute; P2 execute + notify; P1 enqueue proposal, block until approved (v1: CLI approve/reject command + webhook stub for future UI).
- **User contribution point:** the severity-classification guardrail — deterministic floor/ceiling rules that override the LLM's severity when policy demands it (e.g. anything touching payments ≥ P2). Marked TODO in `approvals.py`.

### Milestone 5 — SSL / DNS / email adapters
- `ssl_adapter.py`: raw `ssl` socket, read `notAfter`, escalate at 30/14/3 days; trigger ACME renewal hook if configured.
- `dns_adapter.py`: registrar API (Cloudflare/Namecheap) expiry + `dnspython` multi-resolver consistency check; escalate-only (60/30/7 days).
- `email_adapter.py`: SES/SendGrid bounce/complaint rate + quota; remediation = throttle non-transactional sends, flip provider config.

### Milestone 6 — Deploy correlation + auto-rollback
- `deploy_adapter.py`: webhook receiver for deploy events (GitHub Actions/Vercel/kubectl apply timestamps); error-rate window correlation in `rules.py`; remediation = `kubectl rollout undo` via the `kubernetes` client against the kind cluster (matches the sandbox choice).

### Milestone 7 — Backup verification
- `backup_adapter.py`: verify job completion + test-restore into a scratch database + checksum; alert-only (no real-time remediation per PRD).

### Milestone 8 — Agent self-monitoring
- `selfmonitor/heartbeat.py`: periodic GET to a configured dead-man's-switch URL.
- `selfmonitor/dashboard.py`: aggregates the audit JSONL into per-customer stats (incidents/mo, LLM spend, autonomous-vs-escalated ratio, MTTD/MTTR) — CLI table output for v1.

### Milestone 9 — Golden-path regression suite + alert-only mode
- `tests/golden/`: for each of the 10 failure modes — run chaos script → assert anomaly fires within its window → assert the reasoning agent proposes the correct action class → assert P-tier routing is honored → assert report generated. LLM calls in CI use a recorded/stub decision layer (`reasoning.py` gets a `FakeReasoner` injection point) so the suite is deterministic; a separate `--live` marker runs against the real API.
- `alert_only: true` config flag: full detect→reason→report path, remediation suppressed — the "earn autonomy" staging tier.

## Security model (cross-cutting, from PRD §10)
- All credentials from env vars referenced in `config/customer.yaml` (never stored in file); per-integration so each is independently revocable.
- Stripe restricted key (read-only unless replay enabled); IAM scoped to the one bucket; dedicated read-only Postgres monitoring role + separate kill-role; registrar read-only token.
- Remediation tool allowlist is explicit per adapter — the reasoning agent can only propose actions that exist in the adapter's registered action set.

## Verification
1. **Unit**: `pytest tests/unit` — rules thresholds, adapter response parsing, report formatting.
2. **End-to-end per failure mode**: `kind create cluster --config sandbox/kind-config.yaml && kubectl apply -f sandbox/k8s/`, seed the fake restaurant app, then e.g. `python -m sandbox.chaos.exhaust_db_pool` and watch the orchestrator log: anomaly → reasoning decision → remediation → incident report. Repeat for all 10 chaos scripts via `pytest tests/golden`.
3. **Live reasoning check**: one `--live` golden test (Stripe mismatch) against the real Claude API to validate the Agent SDK integration and confirm token/cost figures land in the audit log.
4. **Self-monitoring**: stop the orchestrator, confirm the heartbeat URL stops receiving pings; run the dashboard CLI and verify MTTD/MTTR computed from the golden-run audit log.
