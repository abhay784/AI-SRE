"""Chaos #4: SSL certificate about to expire.

Generates a self-signed cert valid for only N days and serves it on
localhost:8443. Point the ssl adapter at host localhost, port 8443 to watch
it fire the expiry rule. Requires the `openssl` CLI.
"""

import argparse
import ssl
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def make_cert(days: int, directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(cert), "-days", str(days), "-nodes",
         "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"almost-expired cert says hi")

    def log_message(self, *args):  # quiet
        pass


def main(days: int, port: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ai-sre-ssl-"))
    cert, key = make_cert(days, tmp)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"serving TLS on :{port} with a cert expiring in {days} days (ctrl-c to stop)")
    print(f"point the ssl adapter at host=localhost port={port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    main(args.days, args.port)
