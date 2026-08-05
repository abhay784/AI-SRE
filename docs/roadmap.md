# From personal monitor to hosted product

The first audience is developers maintaining a few websites and GitHub
repositories. The initial job is to answer: "Is my site responding correctly,
and is the latest completed CI run on my delivery branch failing?" The current
release provides that workflow as a self-hosted CLI.

## 1. Use it on real projects

- Configure your homepage, health endpoint, and test/deploy workflows.
- Run continuously outside the site's hosting environment and connect a
  webhook receiver you control.
- Exercise a controlled failure and recovery. Record when the check changed,
  when detection occurred, and whether the alert reached you.
- Track actionable incidents, false alarms, repeated alerts, failed deliveries,
  and cost before publishing performance claims.

Exit criterion: reproducible failure detection on your own stack and an
operating guide another developer can follow.

## 2. Make a small pilot dependable

- Persist confirmation, incident, and recovery state in SQLite/Postgres.
- Add a durable notification outbox, replay, retention, and delivery metrics.
- Provide native email/chat alerts and maintenance windows.
- Add an authenticated interface for adding targets and inspecting incidents.
- Use a GitHub App with repository-scoped installations for hosted users.

Exit criterion: invited developers can onboard and receive useful alerts
without editing source code or sharing credentials with each other.

## 3. Support hosted users

- Isolate tenants, credentials, jobs, and audit records; encrypt stored secrets.
- Validate target ownership and defend probes against SSRF, including redirects,
  DNS changes, private networks, and cloud metadata endpoints.
- Add authentication, quotas, durable scheduling, API rate-limit handling,
  billing when needed, and monitoring for the service itself.
- Publish data-handling, retention, and support expectations based on operating
  experience.

Exit criterion: tested isolation and recovery, clear operating limits, and
sustainable cost per monitored project.

## Possible GitHub extensions

Consider deployment-status correlation, required PR checks, stalled reviews,
and dependency/security alert summaries after Actions monitoring proves useful.
Each extension should support a specific user decision and request only the
permissions it needs. Automatic reruns, repository writes, and code fixes need
a separate approval and audit design.

These hosted features and extensions are not implemented in this release.
