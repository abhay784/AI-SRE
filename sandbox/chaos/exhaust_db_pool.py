"""Chaos #2: DB connection pool exhaustion.

Opens N connections and holds them idle-in-transaction. With the sandbox's
max_connections=20, the default of 17 pushes usage past the 80% threshold.
Hold time gives the postgres adapter (30s cadence) time to detect and kill.
"""

import argparse
import time


def main(count: int, hold_seconds: int) -> None:
    import psycopg

    from ._common import POSTGRES_DSN

    conns = []
    for i in range(count):
        conn = psycopg.connect(POSTGRES_DSN)
        conn.execute("BEGIN")  # idle-in-transaction: eligible for the kill action
        conn.execute("SELECT 1")
        conns.append(conn)
        print(f"holding connection {i + 1}/{count}")
    print(f"pool loaded; holding for {hold_seconds}s (ctrl-c to release early)")
    try:
        time.sleep(hold_seconds)
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        print("released")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=17)
    parser.add_argument("--hold", type=int, default=600)
    args = parser.parse_args()
    main(args.count, args.hold)
