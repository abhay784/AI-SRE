# AI SRE bot

Autonomous application-layer reliability agent for small business websites.
Detects business-logic failures that edge tools can't see (dropped Stripe
webhooks, S3 policy flips, DB pool exhaustion, lapsing certs/domains, bad
deploys, silent backup failures), reasons about them with Claude, remediates
where safe, and reports in plain English.

## Architecture in one line

```
adapters poll → deterministic rules → (anomaly only) Claude Agent SDK decides
→ severity gate (P3 auto / P2 auto+notify / P1 human approval) → adapter
remediates → owner-friendly incident report + append-only audit log
```

The LLM never executes side effects — it proposes an action from the
adapter's allowlist; the orchestrator executes after gating.

## Quickstart (no cluster needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/           # unit + golden pipeline suite
```

## Full sandbox (kind)

```bash
# 1. Cluster + fake restaurant stack
kind create cluster --name slopsaver --config sandbox/kind-config.yaml
docker build -t slopsaver-sandbox:latest sandbox/
kind load docker-image slopsaver-sandbox:latest --name slopsaver
kubectl apply -f sandbox/k8s/all.yaml

# 2. Install with real-adapter deps and run in alert-only mode
.venv/bin/pip install -e ".[adapters,sandbox]"
cp config/customer.example.yaml config/customer.yaml
.venv/bin/slopsaver run --fake-reasoner        # or drop the flag to use Claude

# 3. Inject a failure (each PRD failure mode has a script)
.venv/bin/python -m sandbox.chaos.spam_forms
.venv/bin/python -m sandbox.chaos.exhaust_db_pool
.venv/bin/python -m sandbox.chaos.flip_s3_policy

# 4. Watch the pipeline
ls var/incidents/                              # owner-facing reports
.venv/bin/slopsaver pending                    # P1 proposals awaiting approval
.venv/bin/slopsaver approve <incident_id>
.venv/bin/slopsaver dashboard                  # cost / MTTD / autonomy stats
```

To use the real reasoning agent, set `ANTHROPIC_API_KEY` (or `ant auth login`)
and run without `--fake-reasoner`. Every LLM call's tokens, cost, latency, and
decision are logged to `var/audit/llm_calls.jsonl`.

## Repo map

| Path | What |
|---|---|
| `slopsaver/core/` | orchestrator, rule engine, reasoning (Claude Agent SDK), approvals, audit, reports |
| `slopsaver/adapters/` | one pluggable adapter per integration (Stripe, Postgres, S3, SSL, DNS, email, traffic, deploy, backup) |
| `slopsaver/selfmonitor/` | dead-man's-switch heartbeat + cost dashboard |
| `sandbox/` | kind cluster, fake restaurant apps, 10 chaos scripts |
| `tests/golden/` | trigger → detect → decide → remediate → report, per failure mode |

## Two decision points marked for you

Search for `TODO(user)`:

1. **`slopsaver/adapters/stripe_adapter.py` — `_charge_matches_order`**: what
   counts as a charge↔order match. Too loose misses real losses; too strict
   pages with false alarms.
2. **`slopsaver/core/approvals.py` — `apply_severity_guardrails`**: the
   deterministic floor on the LLM's severity — the trust policy of the whole
   product (what may ever run unattended).

Both ship with working defaults; the golden tests pin their current behavior.
