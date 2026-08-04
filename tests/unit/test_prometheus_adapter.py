import httpx
import pytest

from slopsaver.adapters.prometheus_adapter import PrometheusAdapter


def response(result_type="scalar", result=None, **payload):
    if result is None:
        result = [1_700_000_000, "0.2"] if result_type == "scalar" else []
    return {"status": "success", "data": {"resultType": result_type, "result": result}, **payload}


def adapter(handler, query=None, **overrides):
    query = {"name": "error-rate", "query": "sum(rate(http_requests_total[5m]))", "operator": "gt",
             "threshold": 0.1, **(query or {})}
    return PrometheusAdapter({"url": "https://prometheus.test/platform", "queries": [query], **overrides},
                             transport=httpx.MockTransport(handler))


async def test_posts_instant_query_with_bound_and_uses_base_path():
    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/platform/api/v1/query"
        body = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        assert body["query"].startswith("sum%28rate")
        assert body["timeout"] == "10s"
        assert body["limit"] == "100"
        return httpx.Response(200, json=response())
    health = await adapter(respond).check_health()
    check = health.observed["queries"][0]
    assert not health.ok
    assert check["value"] == 0.2
    assert "breached gt 0.1" in check["issues"][0]


@pytest.mark.parametrize("operator,threshold,healthy", [
    ("gt", 0.2, True), ("gte", 0.2, False), ("lt", 0.3, False), ("lte", 0.2, False),
])
async def test_threshold_operators(operator, threshold, healthy):
    health = await adapter(lambda r: httpx.Response(200, json=response()),
                           {"operator": operator, "threshold": threshold}).check_health()
    assert health.ok is healthy


async def test_vector_reducers_and_query_timeout():
    payload = response("vector", [
        {"metric": {"instance": "one"}, "value": [1, "0.1"]},
        {"metric": {"instance": "two"}, "value": [1, "0.3"]},
    ])
    def respond(request):
        assert b"timeout=2.5s" in request.content
        return httpx.Response(200, json=payload)
    health = await adapter(respond, {"reducer": "avg", "threshold": 0.19}, query_timeout_seconds=2.5).check_health()
    assert health.observed["queries"][0]["series_count"] == 2
    assert health.observed["queries"][0]["value"] == pytest.approx(0.2)
    assert not health.ok


@pytest.mark.parametrize("result_type,result", [
    ("matrix", []), ("string", [1, "value"]), ("scalar", [1, "NaN"]),
])
async def test_rejects_unsupported_or_nonfinite_results(result_type, result):
    health = await adapter(lambda r: httpx.Response(200, json=response(result_type, result))).check_health()
    assert not health.ok
    assert health.observed["queries"][0]["issues"] == ["Prometheus query returned an invalid response"]


async def test_no_data_is_visible_by_default_and_can_be_allowed():
    default = await adapter(lambda r: httpx.Response(200, json=response("vector", []))).check_health()
    allowed = await adapter(lambda r: httpx.Response(200, json=response("vector", [])),
                            {"no_data_is_failure": False}).check_health()
    assert not default.ok
    assert default.observed["queries"][0]["issues"] == ["query returned no data"]
    assert allowed.ok


@pytest.mark.parametrize("status", [400, 422, 503])
async def test_api_errors_do_not_log_response_or_query_content(status):
    health = await adapter(lambda r: httpx.Response(status, text="private detail")).check_health()
    assert not health.ok
    assert health.observed["queries"][0]["issues"] == [f"Prometheus HTTP {status}"]
    assert "private detail" not in health.model_dump_json()


async def test_bearer_token_is_sent_only_to_prometheus_and_not_recorded(monkeypatch):
    monkeypatch.setenv("PROM_TOKEN", "super-secret")
    def respond(request):
        assert request.headers["Authorization"] == "Bearer super-secret"
        return httpx.Response(200, json=response())
    health = await adapter(respond, bearer_token_env="PROM_TOKEN").check_health()
    assert "super-secret" not in health.model_dump_json()


async def test_missing_configured_bearer_token_fails_without_network(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    health = await adapter(lambda r: pytest.fail("request should not be made"), bearer_token_env="MISSING_TOKEN").check_health()
    assert health.error == "configured bearer token environment variable MISSING_TOKEN is not set"


@pytest.mark.parametrize("overrides", [
    {"url": "file:///etc/passwd"}, {"url": "https://user:secret@prometheus.test"},
    {"url": "https://prometheus.test/?token=secret"}, {"timeout_seconds": 0},
    {"query_timeout_seconds": 11}, {"queries": []},
])
def test_rejects_unsafe_or_invalid_config(overrides):
    with pytest.raises(ValueError):
        PrometheusAdapter({"url": "https://prometheus.test", "queries": [{"name": "up", "query": "up",
                                                                            "operator": "lt", "threshold": 1}],
                           **overrides})
