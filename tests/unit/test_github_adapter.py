import httpx
import pytest

from ai_sre.adapters.github_adapter import GitHubAdapter


def run(conclusion="failure", **extra):
    return {"id": 42, "name": "CI", "conclusion": conclusion, "head_sha": "abc", **extra}


def monitor(handler, repositories=None):
    return GitHubAdapter({"repositories": repositories or [{"repo": "owner/project", "branch": "main",
                                                            "workflows": ["ci.yml"]}]},
                         transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("conclusion,healthy", [("failure", False), ("timed_out", False),
    ("action_required", False), ("success", True), ("cancelled", True), ("skipped", True)])
async def test_latest_completed_run_is_scoped_to_branch(conclusion, healthy, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    def respond(request):
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer private-token"
        assert dict(request.url.params) == {"branch": "main", "status": "completed", "per_page": "1"}
        return httpx.Response(200, json={"workflow_runs": [run(conclusion)]})
    health = await monitor(respond).check_health()
    assert health.ok is healthy
    assert "private-token" not in health.model_dump_json()
    assert health.observed["repositories"][0]["runs"][0]["url"].endswith("/actions/runs/42")


async def test_workflow_discovery_paginates_and_skips_disabled():
    paths = []
    def respond(request):
        paths.append(str(request.url))
        if request.url.path.endswith("/workflows"):
            page = request.url.params["page"]
            return httpx.Response(200, json={"workflows": [
                {"id": int(page), "state": "active"}, {"id": 999, "state": "disabled_manually"}]},
                headers={"Link": '<https://api.github.com/repos/owner/project/actions/workflows?page=2>; rel="next"'} if page == "1" else {})
        return httpx.Response(200, json={"workflow_runs": [run("success")]})
    health = await monitor(respond, [{"repo": "owner/project", "branch": "main"}]).check_health()
    assert health.ok
    assert len(health.observed["repositories"][0]["runs"]) == 2
    assert not any("/999/" in p for p in paths)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_api_errors_are_visible_without_response_secrets(status):
    health = await monitor(lambda r: httpx.Response(status, text="secret")).check_health()
    assert not health.ok
    assert str(status) in health.observed["repositories"][0]["error"]
    assert "secret" not in health.model_dump_json()


async def test_one_inaccessible_repository_does_not_hide_another_failure():
    def respond(request):
        return httpx.Response(404) if "/private/" in request.url.path else httpx.Response(200, json={"workflow_runs": [run()]})
    health = await monitor(respond, [{"repo": f"owner/{name}", "branch": "main", "workflows": ["ci.yml"]}
                                    for name in ["private", "public"]]).check_health()
    assert len(health.observed["repositories"]) == 2
    assert health.observed["repositories"][0]["error"]
    assert not health.observed["repositories"][1]["runs"][0]["ok"]


async def test_no_runs_reports_coverage_and_adapter_rejects_writes():
    adapter = monitor(lambda r: httpx.Response(200, json={"workflow_runs": []}))
    health = await adapter.check_health()
    assert health.observed["repositories"][0]["runs"] == []
    assert not health.ok
    assert "No completed runs" in health.observed["repositories"][0]["error"]
    assert not (await adapter.remediate("rerun_workflow", {})).ok


async def test_empty_inventory_is_not_healthy():
    health = await monitor(lambda r: httpx.Response(200, json={"workflows": []}),
                           [{"repo": "owner/project", "branch": "main"}]).check_health()
    assert not health.ok
    assert "No active workflows" in health.observed["repositories"][0]["error"]
