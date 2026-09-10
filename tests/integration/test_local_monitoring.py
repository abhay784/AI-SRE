"""Exercise real HTTP probes and webhook delivery against a loopback server."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slopsaver.adapters.website_adapter import WebsiteAdapter
from slopsaver.core.notifications import WebhookNotifier
from slopsaver.core.reasoning import FakeReasoner
from slopsaver.core.scheduler import Orchestrator


async def test_outage_and_recovery_deliver_real_http_webhooks(tmp_path):
    state = {"status": 503, "events": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(b"ready")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["events"].append(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    adapter = WebsiteAdapter({"targets": [{"name": "storefront", "url": url + "/health",
                                          "contains": "ready"}]})
    orch = Orchestrator({"website": adapter}, FakeReasoner(), alert_only=True,
                        var_root=str(tmp_path), notifier=WebhookNotifier(url + "/alerts"))
    try:
        assert await orch.poll_once(adapter) == []
        incident, = await orch.poll_once(adapter)
        assert incident.status == "escalated"
        state["status"] = 200
        recovery, = await orch.poll_once(adapter)
        assert recovery.status == "recovered"
        assert [e["event"] for e in state["events"]] == ["incident.created", "incident.recovered"]
        assert all(r["delivered"] for r in orch.audit.read("notifications.jsonl"))
        assert len(list((tmp_path / "incidents").glob("*.md"))) == 2
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
