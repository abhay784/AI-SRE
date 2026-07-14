"""Chaos #9: bad deploy corrupting the app.

Records a deploy event, then cranks the gateway's fail_rate so ~half of
customer requests 500. Also sends background traffic so the error rate is
actually measurable. The deploy adapter should correlate the spike with the
deploy timestamp and propose a rollback (P1 — waits for approval).

In the full kind flow, use --kubectl to do a real `kubectl set image` so the
rollback action has a revision to undo.
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ._common import GATEWAY

DEPLOY_STATE = Path("var/deploy_state.json")


def main(fail_rate: float, use_kubectl: bool, traffic: int) -> None:
    DEPLOY_STATE.parent.mkdir(parents=True, exist_ok=True)
    DEPLOY_STATE.write_text(json.dumps({
        "deployment": "api-gateway",
        "namespace": "default",
        "deployed_at": datetime.now(timezone.utc).isoformat(),
    }))
    print(f"deploy event recorded in {DEPLOY_STATE}")

    if use_kubectl:
        subprocess.run(
            ["kubectl", "set", "env", "deployment/api-gateway", f"FAIL_RATE={fail_rate}"],
            check=True,
        )
        print(f"kubectl: api-gateway redeployed with FAIL_RATE={fail_rate}")
    else:
        with httpx.Client(timeout=10) as client:
            client.post(f"{GATEWAY}/admin", json={"fail_rate": fail_rate})
        print(f"gateway fail_rate set to {fail_rate}")

    with httpx.Client(timeout=10) as client:
        errors = sum(
            1 for _ in range(traffic)
            if client.get(f"{GATEWAY}/menu/margherita").status_code >= 500
        )
    print(f"background traffic sent: {errors}/{traffic} errored — watch for the bad_deploy anomaly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-rate", type=float, default=0.5)
    parser.add_argument("--kubectl", action="store_true")
    parser.add_argument("--traffic", type=int, default=100)
    args = parser.parse_args()
    main(args.fail_rate, args.kubectl, args.traffic)
