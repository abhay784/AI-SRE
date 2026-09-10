"""AI-SRE CLI.

  ai-sre run --config config/customer.yaml [--alert-only] [--fake-reasoner]
  ai-sre pending                      # list P1 proposals awaiting approval
  ai-sre approve <incident_id>        # execute an approved P1 remediation
  ai-sre reject <incident_id>
  ai-sre dashboard                    # cost / incident stats from the audit log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import yaml
from dotenv import load_dotenv

from .core.approvals import ApprovalQueue
from .core.models import HealthResult
from .core.audit import AuditLog
from .core.registry import build_adapters
from .core.notifications import WebhookNotifier
from .core.reasoning import ClaudeReasoner, FakeReasoner
from .core.rules import RuleEngine
from .core.scheduler import Orchestrator
from .selfmonitor.dashboard import render
from .selfmonitor.heartbeat import heartbeat_loop


def _load_config(path: str) -> dict:
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError("configuration must be a YAML mapping")
    return config


def _build_orchestrator(config: dict, *, alert_only: bool, fake_reasoner: bool) -> Orchestrator:
    adapters = build_adapters(config)
    var_root = config.get("state_dir", "var")
    audit = AuditLog(f"{var_root}/audit")
    if config.get("reasoner", "claude") not in {"claude", "fake", "deterministic"}:
        raise ValueError("reasoner must be deterministic, fake, or claude")
    deterministic = fake_reasoner or config.get("reasoner") in {"fake", "deterministic"}
    if deterministic:
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
        var_root=var_root,
        notifier=WebhookNotifier.from_config(config),
    )
    if not deterministic:
        holder["orch"] = orch
    return orch


async def _run(config: dict, alert_only: bool, fake_reasoner: bool) -> None:
    orch = _build_orchestrator(config, alert_only=alert_only, fake_reasoner=fake_reasoner)
    tasks = [orch.run()]
    if url := config.get("heartbeat_url"):
        tasks.append(heartbeat_loop(url, float(config.get("heartbeat_interval", 60))))
    mode = "ALERT-ONLY" if orch.alert_only else "autonomous (P1 gated)"
    logging.info("AI-SRE running in %s mode with adapters: %s",
                 mode, ", ".join(orch.adapters))
    await asyncio.gather(*tasks)


async def _check(config: dict, *, as_json: bool = False) -> int:
    """Read-only preflight; no reasoning, notifications, or remediation."""
    adapters = build_adapters(config)
    async def probe(adapter):
        try:
            return await adapter.check_health()
        except Exception as exc:
            return HealthResult(adapter=adapter.name, ok=False,
                                error=f"collector raised {type(exc).__name__}")
    results = await asyncio.gather(*(probe(a) for a in adapters.values()))
    if as_json:
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
    else:
        for result in results:
            print(f"{'OK' if result.ok and not result.error else 'FAIL'}  {result.adapter}")
            if result.error:
                print(f"  {result.error}")
            for target in result.observed.get("targets", []):
                detail = "; ".join(target["issues"]) or f"HTTP {target['status_code']}, {target['latency_ms']} ms"
                print(f"  {'OK' if target['ok'] else 'FAIL'}  {target['name']}: {detail}")
            for repo in result.observed.get("repositories", []):
                print(f"  {repo['repo']} ({repo['branch']}): {repo.get('error') or str(len(repo['runs'])) + ' completed workflow(s) checked'}")
                for run in repo["runs"]:
                    print(f"    {run['workflow']}: {run['conclusion']} — {run['url']}")
    return 0 if all(r.ok and not r.error for r in results) else 1


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
    # HTTP client request logs include complete URLs, including secret query strings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="ai-sre")
    parser.add_argument("--config", default="config/customer.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="start the monitoring orchestrator")
    run_p.add_argument("--alert-only", action="store_true")
    run_p.add_argument("--fake-reasoner", action="store_true",
                       help="use deterministic decisions instead of the Claude API")
    check_p = sub.add_parser("check", help="probe once without reasoning or remediation (exit 1 if unhealthy)")
    check_p.add_argument("--json", action="store_true", help="print structured health results")

    sub.add_parser("pending", help="list P1 proposals awaiting approval")
    approve_p = sub.add_parser("approve", help="approve and execute a pending P1 remediation")
    approve_p.add_argument("incident_id")
    approve_p.add_argument("--fake-reasoner", action="store_true")
    reject_p = sub.add_parser("reject", help="reject a pending P1 remediation")
    reject_p.add_argument("incident_id")
    sub.add_parser("dashboard", help="print cost/incident stats")

    # Accept --config on either side of the subcommand.
    for command_parser in sub.choices.values():
        command_parser.add_argument("--config", default=argparse.SUPPRESS)

    args = parser.parse_args()
    try:
        config = _load_config(args.config)
    except FileNotFoundError as exc:
        if args.config == "config/customer.yaml" and args.command in {"pending", "reject", "dashboard"}:
            config = {}  # Preserve the original local audit/approval CLI without config.
        else:
            parser.error(f"cannot load configuration: {exc}. Start with config/monitoring.example.yaml")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(f"cannot load configuration: {exc}. Start with config/monitoring.example.yaml")
    var_root = config.get("state_dir", "var")

    if args.command == "run":
        try:
            asyncio.run(_run(config, args.alert_only, args.fake_reasoner))
        except KeyboardInterrupt:
            pass
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "check":
        try:
            sys.exit(asyncio.run(_check(config, as_json=args.json)))
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "pending":
        for incident in ApprovalQueue(f"{var_root}/approvals").pending():
            a = incident.action
            print(f"{incident.id}  {incident.anomaly.anomaly_type:24} "
                  f"{a.adapter}/{a.action}  ({a.severity.value})  {a.rationale[:60]}")
    elif args.command == "approve":
        sys.exit(asyncio.run(_approve(config, args.incident_id, args.fake_reasoner)))
    elif args.command == "reject":
        incident = ApprovalQueue(f"{var_root}/approvals").resolve(args.incident_id, approved=False)
        print("rejected" if incident else f"no pending approval with id {args.incident_id}")
    elif args.command == "dashboard":
        print(render(AuditLog(f"{var_root}/audit")))


if __name__ == "__main__":
    main()
