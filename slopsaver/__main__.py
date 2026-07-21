"""SlopSaver CLI.

  slopsaver run --config config/customer.yaml [--alert-only] [--fake-reasoner]
  slopsaver pending                      # list P1 proposals awaiting approval
  slopsaver approve <incident_id>        # execute an approved P1 remediation
  slopsaver reject <incident_id>
  slopsaver dashboard                    # cost / incident stats from the audit log
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import yaml
from dotenv import load_dotenv

from .core.approvals import ApprovalQueue
from .core.audit import AuditLog
from .core.registry import build_adapters
from .core.reasoning import ClaudeReasoner, FakeReasoner
from .core.rules import RuleEngine
from .core.scheduler import Orchestrator
from .selfmonitor.dashboard import render
from .selfmonitor.heartbeat import heartbeat_loop


def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _build_orchestrator(config: dict, *, alert_only: bool, fake_reasoner: bool) -> Orchestrator:
    adapters = build_adapters(config)
    audit = AuditLog("var/audit")
    if fake_reasoner or config.get("reasoner") == "fake":
        reasoner = FakeReasoner()
    else:
        # context provider is bound after the orchestrator exists (circular);
        # use a late-binding closure.
        holder: dict = {}

        async def context_provider(name: str) -> dict:
            return await holder["orch"].get_adapter_context(name)

        reasoner = ClaudeReasoner(context_provider, audit)
    orch = Orchestrator(
        adapters, reasoner,
        rules=RuleEngine(config.get("thresholds")),
        audit=audit,
        alert_only=alert_only or bool(config.get("alert_only")),
    )
    if not (fake_reasoner or config.get("reasoner") == "fake"):
        holder["orch"] = orch
    return orch


async def _run(config: dict, alert_only: bool, fake_reasoner: bool) -> None:
    orch = _build_orchestrator(config, alert_only=alert_only, fake_reasoner=fake_reasoner)
    tasks = [orch.run()]
    if url := config.get("heartbeat_url"):
        tasks.append(heartbeat_loop(url, float(config.get("heartbeat_interval", 60))))
    mode = "ALERT-ONLY" if orch.alert_only else "autonomous (P1 gated)"
    logging.info("SlopSaver running in %s mode with adapters: %s",
                 mode, ", ".join(orch.adapters))
    await asyncio.gather(*tasks)


async def _approve(config: dict, incident_id: str, fake_reasoner: bool) -> int:
    orch = _build_orchestrator(config, alert_only=False, fake_reasoner=fake_reasoner)
    incident = await orch.execute_approved(incident_id)
    if incident is None:
        print(f"no pending approval with id {incident_id}")
        return 1
    print(f"{incident.id}: {incident.status} — {incident.result.detail if incident.result else ''}")
    return 0


def main() -> None:
    # Loads .env from the current directory (or nearest parent) if present;
    # a no-op if it's absent, so nothing breaks for users using real env vars
    # or `ant auth login` instead. Real shell/CI env vars always win — dotenv
    # never overrides an already-set variable.
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="slopsaver")
    parser.add_argument("--config", default="config/customer.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="start the monitoring orchestrator")
    run_p.add_argument("--alert-only", action="store_true")
    run_p.add_argument("--fake-reasoner", action="store_true",
                       help="use deterministic decisions instead of the Claude API")

    sub.add_parser("pending", help="list P1 proposals awaiting approval")
    approve_p = sub.add_parser("approve", help="approve and execute a pending P1 remediation")
    approve_p.add_argument("incident_id")
    approve_p.add_argument("--fake-reasoner", action="store_true")
    reject_p = sub.add_parser("reject", help="reject a pending P1 remediation")
    reject_p.add_argument("incident_id")
    sub.add_parser("dashboard", help="print cost/incident stats")

    args = parser.parse_args()

    if args.command == "run":
        config = _load_config(args.config)
        asyncio.run(_run(config, args.alert_only, args.fake_reasoner))
    elif args.command == "pending":
        for incident in ApprovalQueue().pending():
            a = incident.action
            print(f"{incident.id}  {incident.anomaly.anomaly_type:24} "
                  f"{a.adapter}/{a.action}  ({a.severity.value})  {a.rationale[:60]}")
    elif args.command == "approve":
        config = _load_config(args.config)
        sys.exit(asyncio.run(_approve(config, args.incident_id, args.fake_reasoner)))
    elif args.command == "reject":
        incident = ApprovalQueue().resolve(args.incident_id, approved=False)
        print("rejected" if incident else f"no pending approval with id {args.incident_id}")
    elif args.command == "dashboard":
        print(render(AuditLog("var/audit")))


if __name__ == "__main__":
    main()
