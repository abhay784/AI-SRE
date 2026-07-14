"""Chaos #8: email deliverability failure.

Pushes the gateway's fake provider metrics into bounce-spike / quota-limit
territory. The email adapter should propose throttle_noncritical.
"""

import argparse

import httpx

from ._common import GATEWAY


def main(bounce_rate: float, quota_pct: float) -> None:
    with httpx.Client(timeout=10) as client:
        client.post(f"{GATEWAY}/admin", json={
            "email_metrics": {"bounce_rate": bounce_rate, "quota_used_pct": quota_pct},
        })
    print(f"fake email metrics set: bounce_rate={bounce_rate}, quota_used_pct={quota_pct}")
    print("undo: python -m sandbox.chaos.degrade_email --bounce 0.01 --quota 12")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bounce", type=float, default=0.12)
    parser.add_argument("--quota", type=float, default=95.0)
    args = parser.parse_args()
    main(args.bounce, args.quota)
