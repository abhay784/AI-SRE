"""Read-only GitHub Actions monitoring, scoped to repository and branch."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class RepositoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = Field(min_length=1)
    workflows: list[str] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("use owner/repository, not relative paths")
        return value

    @field_validator("workflows")
    @classmethod
    def validate_workflows(cls, values: list[str]) -> list[str]:
        if any(not v or "/" in v or v in {".", ".."} for v in values):
            raise ValueError("workflows must be file names (e.g. ci.yml) or workflow IDs")
        return list(dict.fromkeys(values))


class GitHubAdapter(BaseAdapter):
    name = "github"
    default_interval = 300.0
    ACTIONS = {}
    FAILURES = {"failure", "timed_out", "action_required", "stale"}

    def __init__(self, config: dict, *, transport=None):
        super().__init__(config)
        self.targets = [RepositoryTarget.model_validate(t) for t in config.get("repositories", [])]
        if not self.targets or len({(t.repo.lower(), t.branch) for t in self.targets}) != len(self.targets):
            raise ValueError("github.repositories must contain unique repository/branch pairs")
        self.transport = transport

    async def _workflows(self, client, target) -> list[str]:
        if target.workflows:
            return target.workflows
        workflows = []
        # Follow pagination, but do not silently call an incomplete inventory healthy.
        for page in range(1, 11):
            response = await client.get(f"/repos/{target.repo}/actions/workflows",
                                        params={"per_page": 100, "page": page})
            response.raise_for_status()
            workflows.extend(str(w["id"]) for w in response.json()["workflows"] if w["state"] == "active")
            if "next" not in response.links:
                return workflows
        raise ValueError("workflow inventory exceeded 1000 entries; configure explicit workflows")

    async def check_health(self) -> HealthResult:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "AI-SRE/0.2"}
        token = os.getenv(self.config.get("token_env", "GITHUB_TOKEN"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        repositories = []
        async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers,
                                     timeout=15, transport=self.transport) as client:
            for target in self.targets:
                record = {"resource_id": f"{target.repo}@{target.branch}", "repo": target.repo,
                          "branch": target.branch, "runs": [], "ok": True}
                try:
                    workflows = await self._workflows(client, target)
                    record["workflows_checked"] = len(workflows)
                    missing = []
                    for workflow in workflows:
                        # Inspect the latest completed run even while its successor is running.
                        response = await client.get(
                            f"/repos/{target.repo}/actions/workflows/{quote(workflow, safe='')}/runs",
                            params={"branch": target.branch, "status": "completed", "per_page": 1},
                        )
                        response.raise_for_status()
                        runs = response.json()["workflow_runs"]
                        if not runs:
                            missing.append(workflow)
                            continue
                        run = runs[0]
                        record["runs"].append({
                            "resource_id": f"{target.repo}@{target.branch}:{workflow}",
                            "repo": target.repo, "branch": target.branch,
                            "workflow": run.get("name") or workflow, "workflow_id": workflow,
                            "run_id": run["id"], "run_attempt": run.get("run_attempt", 1),
                            "conclusion": run["conclusion"], "head_sha": run.get("head_sha"),
                            "url": f"https://github.com/{target.repo}/actions/runs/{run['id']}",
                            "ok": run["conclusion"] not in self.FAILURES,
                        })
                    record["ok"] = all(r["ok"] for r in record["runs"])
                    if not workflows:
                        record.update(ok=False, error="No active workflows found; enable GitHub Actions or select workflows")
                    elif missing:
                        record.update(ok=False, error="No completed runs on the configured branch for: " + ", ".join(missing))
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    detail = "check repository, branch, and Actions: read token access"
                    if status in {403, 429}:
                        detail = "check token permissions or API rate limits; increase the polling interval"
                    record.update(ok=False, error=f"GitHub HTTP {status}: {detail}")
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    record.update(ok=False, error=f"GitHub collection failed ({type(exc).__name__})")
                repositories.append(record)
        return HealthResult(adapter=self.name, ok=all(r["ok"] for r in repositories),
                            observed={"repositories": repositories})

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        return self._unknown_action(action)
