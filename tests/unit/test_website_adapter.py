import httpx
import pytest

from slopsaver.adapters.website_adapter import WebsiteAdapter


def adapter(handler, **overrides):
    return WebsiteAdapter({"targets": [{"name": "site", "url": "https://site.test/?token=secret",
                                        **overrides}]}, transport=httpx.MockTransport(handler))


async def test_checks_status_content_and_redacts_query():
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, text="ready")
    health = await adapter(respond, contains="ready").check_health()
    assert health.ok
    target = health.observed["targets"][0]
    assert target["content_matched"]
    assert target["url"] == "https://site.test/"
    assert "secret" not in health.model_dump_json()


@pytest.mark.parametrize("status,text,expected", [
    (503, "ready", "got 503"), (200, "broken", "expected text missing"),
])
async def test_unhealthy_status_and_content(status, text, expected):
    health = await adapter(lambda r: httpx.Response(status, text=text), contains="ready").check_health()
    assert not health.ok
    assert expected in "; ".join(health.observed["targets"][0]["issues"])


async def test_timeout_and_connection_failure_do_not_expose_url():
    for exception in (httpx.ReadTimeout, httpx.ConnectError):
        def fail(request):
            raise exception("https://site.test/?token=secret", request=request)
        health = await adapter(fail).check_health()
        assert not health.ok
        assert "secret" not in health.model_dump_json()


async def test_slow_response(monkeypatch):
    times = iter([0, 3])
    monkeypatch.setattr("slopsaver.adapters.website_adapter.time", type("Clock", (), {
        "monotonic": staticmethod(lambda: next(times))}))
    health = await adapter(lambda r: httpx.Response(200)).check_health()
    assert not health.ok
    assert health.observed["targets"][0]["latency_ms"] == 3000


async def test_follows_redirects():
    def respond(request):
        return httpx.Response(302, headers={"Location": "/ready"}) if request.url.path == "/" else httpx.Response(200)
    assert (await adapter(respond).check_health()).ok


async def test_bounds_content_scan():
    health = await adapter(lambda r: httpx.Response(200, content=b"x" * (1048576 + 1) + b"ready"),
                           contains="ready").check_health()
    assert not health.ok


async def test_one_failed_target_does_not_hide_healthy_target():
    monitor = WebsiteAdapter({"targets": [{"name": n, "url": f"https://site.test/{n}"}
                                         for n in ["up", "down"]]}, transport=httpx.MockTransport(
                                             lambda r: httpx.Response(200 if r.url.path == "/up" else 503)))
    health = await monitor.check_health()
    assert [t["ok"] for t in health.observed["targets"]] == [True, False]
    assert not (await monitor.remediate("restart", {})).ok


@pytest.mark.parametrize("overrides", [{"url": "file:///etc/passwd"}, {"url": "https://user:secret@site.test"},
                                       {"timeout_seconds": 0}, {"max_latency_ms": float("nan")},
                                       {"failure_threshold": 0}, {"typo": True}])
def test_rejects_invalid_targets(overrides):
    with pytest.raises(ValueError):
        adapter(lambda r: httpx.Response(200), **overrides)
