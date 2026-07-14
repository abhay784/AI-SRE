"""Chaos #5: domain registration about to lapse.

Sets the gateway's fake-registrar expiry to N days out. The dns adapter
(pointed at http://localhost:30080/registrar/domain) should escalate.
"""

import argparse
from datetime import datetime, timedelta, timezone

import httpx

from ._common import GATEWAY


def main(days: int) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with httpx.Client(timeout=10) as client:
        client.post(f"{GATEWAY}/admin", json={"registrar_expires_at": expires})
    print(f"fake registrar now reports domain expiry in {days} days ({expires})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    main(parser.parse_args().days)
