# Running AI-SRE for your projects

Copy `config/monitoring.example.yaml` to `config/customer.yaml`. Keep
`alert_only: true` and `reasoner: deterministic`. Both new adapters expose an
empty action allowlist and only read targets, even if alert-only mode is
disabled. Only the optional notification receiver receives POST requests.

## Website configuration

Each item under `integrations.website.targets` requires a unique `name` and an
absolute HTTP(S) `url`. Monitor targets you own or have permission to check.
Local/private URLs are supported for self-hosting; do not expose this collector
directly to untrusted customer input.

| Field | Default | Meaning |
| --- | --- | --- |
| `expected_status` | `200` | Exact expected final HTTP status |
| `contains` | unset | Case-sensitive text required in the first 1 MiB of the decoded response |
| `max_latency_ms` | `2000` | Limit for headers, redirects, and any configured content scan |
| `timeout_seconds` | `10` | Total deadline for a probe, including redirects and content scanning |
| `failure_threshold` | `2` | Consecutive failing polls before opening an incident |

Probes follow at most five redirects, verify TLS, and run up to five targets
concurrently. Observations include status, timing, and matching results;
response bodies and URL query strings are not stored. Keep secrets out of
names and URL paths. Embedded usernames/passwords are rejected. Authenticated
probes and browser journeys are not implemented.

`interval` (default 60 seconds) is the delay after a polling cycle finishes.
Two failed polls therefore do not imply an exact two-minute detection SLA.
`ai-sre check` reports current observed health immediately without confirmation
history, incident creation, reasoning, or notification delivery. `--json`
returns structured observations; exit codes are 0 for observed healthy checks,
1 for unhealthy checks, and 2 for configuration errors.

## GitHub configuration

Each item under `integrations.github.repositories` requires `repo: owner/name`
and an explicit `branch`. Set `workflows: [ci.yml, deploy.yml]` to choose
workflow file names or IDs. Omit it to discover active workflows, up to 1,000;
an incomplete inventory produces a collection error.

For each workflow, AI-SRE reads the latest **completed** run on that branch.
An earlier failure remains visible while its successor is queued or running.
`failure`, `timed_out`, `action_required`, and `stale` trigger incidents;
`cancelled`, `neutral`, and `skipped` do not. A newer non-failing completed
outcome, including cancellation/skipping, clears the previous failure.

There is no age cutoff: the latest completed outcome remains relevant until
superseded. Missing active workflows or completed runs are reported as collection
errors, so a typo in the branch name does not look healthy. Check output shows
how many workflows have results. Run each selected workflow on that branch at
least once before expecting healthy monitoring.

Public repositories work without a token. For private repositories, use a
fine-grained token restricted to your repositories with **Actions: read** and
set `GITHUB_TOKEN` in `.env` or the environment. `token_env` can select another
variable. See [GitHub's workflow-run API permissions](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow).

The API host is fixed to `api.github.com`. Tokens are not sent to website
targets or API-provided links. No issues are opened, runs restarted, or settings
changed. HTTP 401/403/404/429 collection errors create incidents.

Each cycle uses one request per selected workflow plus discovery pages when
enabled. Choose explicit workflows and longer intervals to reduce API usage.
Rate-limit errors wait for the normal next cycle; adaptive scheduling is not
implemented. See [GitHub's rate-limit documentation](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

## Prometheus configuration

AI-SRE connects to the stable Prometheus instant-query endpoint,
`/api/v1/query`, which accepts PromQL and returns JSON. Prometheus itself is
free, Apache 2.0-licensed software that you can self-host. Managed Prometheus
services are separate products and may charge for storage, ingestion, or query
usage. The adapter also works with Prometheus-compatible query endpoints when
they support this API shape.

```yaml
prometheus:
  url: https://prometheus.example.com
  interval: 60
  bearer_token_env: PROMETHEUS_BEARER_TOKEN # omit when the endpoint has no auth
  queries:
    - name: api-error-rate
      query: sum(rate(http_requests_total{job="api",code=~"5.."}[5m])) / sum(rate(http_requests_total{job="api"}[5m]))
      operator: gt
      threshold: 0.05
      reducer: max
      severity: P1
      failure_threshold: 2
    - name: api-p95-latency-seconds
      query: histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[5m])))
      operator: gt
      threshold: 0.8
      severity: P2
```

Queries must return an instant scalar or vector. Vector results are reduced to
one value before comparison: `max` is the default; `min`, `sum`, and `avg` are
also supported. Prefer PromQL that aggregates deliberately, and use the reducer
only to make fallback behavior explicit. Requests are capped at 100 series, one
mebibyte of response data, and a configurable timeout (10 seconds by default).
Range-query results, non-finite samples, HTTP errors, and invalid responses are
recorded as query failures.

`operator` is one of `gt`, `gte`, `lt`, or `lte`. An alert opens when the
reduced value satisfies its operator/threshold condition. Empty results fail by
default because missing telemetry can hide an outage. Set `no_data_is_failure:
false` only when an absent series is explicitly expected. `failure_threshold`
controls consecutive failing polls before an incident; it defaults to two.

Use `severity: P1`, `P2`, or `P3` to set incident severity. Prometheus
monitoring is read-only: it has no remediation actions and sends no write
requests to Prometheus. A configured bearer token is read from the environment,
used only for the Prometheus endpoint, and excluded from incident reports and
audit records. Use HTTPS and enforce authentication at a reverse proxy or a
compatible endpoint.

See Prometheus’s [HTTP API documentation](https://prometheus.io/docs/prometheus/latest/querying/api/)
and [license information](https://prometheus.io/docs/introduction/faq/#what-is-the-license-of-prometheus).

## Incident lifecycle and storage

`state_dir` defaults to `var`, relative to the working directory:

- `incidents/*.md`: failure and recovery reports.
- `audit/actions.jsonl`: incident decisions and outcomes.
- `audit/notifications.jsonl`: webhook delivery results.
- `audit/llm_calls.jsonl`: successful AI decisions and usage when enabled.
- `approvals/`: legacy remediation proposals awaiting approval.

Repeated failures for a website/workflow are suppressed for 30 minutes, then
persistent conditions produce reminder incidents. Independent targets do not
suppress each other. A healthy observation after a confirmed failure emits
recovery and permits a fresh incident if the target fails again.

These transitions use in-memory state. Restarts reset confirmation and
deduplication and lose the earlier recovery relationship. Reports/audit files
persist; they are not a durable incident-state database. Use one process per
state directory and archive growing audit files periodically.

## Webhook alerts

Set `AI_SRE_WEBHOOK_URL` in `.env` and add:

```yaml
notifications:
  webhook_url_env: AI_SRE_WEBHOOK_URL
```

The receiver must accept JSON POST and return 2xx. The payload has `event`
(`incident.created` or `incident.recovered`) and `incident` (the serialized
incident model, with ID, anomaly/context, action, result, and status).
`Idempotency-Key` contains the incident ID and stays the same across retries;
receivers should deduplicate it.

This generic schema needs a translating receiver for Slack/Discord incoming
webhooks. Native email/chat integrations and webhook signing are not included.
Use HTTPS for external receivers; URLs may contain receiver credentials and
are excluded from logs.

Network failures, 429, and 5xx responses receive up to three attempts with
short backoff. Other non-2xx responses stop delivery. Redirects are not followed.
Failed delivery is logged locally without erasing the incident or claiming
success. Retries do not survive restarts and have no automatic replay after
all attempts fail.

## Continuous operation

Copy/edit config and `.env`, then run from the repository root:

```bash
docker compose up -d --build
docker compose exec monitor ai-sre check
docker compose exec monitor ai-sre dashboard
docker compose logs --tail=100 monitor
docker compose cp monitor:/app/var/incidents ./incident-reports
```

The image runs as a non-root user and installs the base monitoring package.
The named volume preserves local reports/audits and the restart policy restarts
an exited process. Keep `state_dir: var` with the provided Compose file.
`docker compose down` preserves the volume; adding `-v` deletes it.

Run outside the website's failure domain. An optional `heartbeat_url` can ping
an external dead-man's-switch. Separate users need separate deployments,
credentials, and storage today. A public multi-user service first needs tenant
isolation, protected target validation, authentication, and durable state.

## Optional AI analysis

```bash
python -m pip install '.[ai]'
# Set ANTHROPIC_API_KEY and change reasoner: deterministic to reasoner: claude.
ai-sre run --config config/customer.yaml
```

The original Claude Agent SDK integration proposes decisions after anomalies;
reasoning failures fall back to deterministic decisions. When enabled,
configured resource names and repository metadata are sent to the model
provider. The new collectors exclude website bodies and GitHub tokens.
Recoveries never require AI. The base Docker image omits the AI extra.

`slopsaver` remains an alias for all `ai-sre` commands. `--config PATH` works
before or after the subcommand.
