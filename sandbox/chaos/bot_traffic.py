"""Chaos #6: bot/scraper hammering an app endpoint.

Sends a burst of requests with a spoofed X-Forwarded-For so the gateway's
per-IP counters light up. The traffic adapter should propose block_ip.
"""

import argparse

import httpx

from ._common import GATEWAY


def main(ip: str, count: int) -> None:
    with httpx.Client(timeout=10, headers={"x-forwarded-for": ip}) as client:
        blocked = 0
        for i in range(count):
            resp = client.get(f"{GATEWAY}/menu/margherita")
            if resp.status_code == 403:
                blocked += 1
                if blocked == 1:
                    print(f"request {i + 1}: 403 — the block is in place")
    print(f"sent {count} requests from spoofed {ip}; {blocked} were blocked")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default="203.0.113.66")
    parser.add_argument("--count", type=int, default=900)
    args = parser.parse_args()
    main(args.ip, args.count)
