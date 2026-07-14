"""Chaos #7: reservation-form spam.

Floods /reserve with low-effort submissions. The traffic adapter should
propose enable_captcha; once the gateway requires a captcha token, these
same requests start failing with 400.
"""

import argparse

import httpx

from ._common import GATEWAY


def main(count: int) -> None:
    rejected = 0
    with httpx.Client(timeout=10) as client:
        for i in range(count):
            resp = client.post(f"{GATEWAY}/reserve",
                               json={"name": f"totally-real-person-{i}", "party": 2})
            if resp.status_code == 400:
                rejected += 1
    print(f"sent {count} spam reservations; {rejected} rejected (captcha active)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=60)
    main(parser.parse_args().count)
