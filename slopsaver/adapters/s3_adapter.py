"""S3 bucket misconfiguration adapter (PRD failure #3).

Detection: synthetic fetch of a known object + diff of bucket policy/ACL
against a stored last-known-good baseline. Works against real S3 or MinIO
(set endpoint_url).

Remediation: revert the policy to the stored baseline — the only write this
adapter is allowed to make.

Config:
  bucket: bucket name
  canary_object: key of a known object to fetch (e.g. menu image)
  expected_content_type: content-type the canary should serve
  endpoint_url: optional (MinIO / S3-compatible)
  baseline_path: where the last-known-good policy is stored
                 (default var/baselines/s3_<bucket>.json)
  region: optional
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class S3Adapter(BaseAdapter):
    name = "s3"
    default_interval = 120.0
    ACTIONS = {
        "revert_policy": "Restore the bucket policy to the stored last-known-good baseline",
    }

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.config.get("endpoint_url"),
            region_name=self.config.get("region"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )

    def _baseline_path(self) -> Path:
        default = f"var/baselines/s3_{self.config['bucket']}.json"
        return Path(self.config.get("baseline_path", default))

    def _current_policy(self, s3) -> dict | None:
        try:
            return json.loads(s3.get_bucket_policy(Bucket=self.config["bucket"])["Policy"])
        except s3.exceptions.from_code("NoSuchBucketPolicy"):
            return None
        except Exception as exc:
            if "NoSuchBucketPolicy" in str(exc):
                return None
            raise

    async def check_health(self) -> HealthResult:
        try:
            return await asyncio.to_thread(self._poll)
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))

    def _poll(self) -> HealthResult:
        s3 = self._client()
        bucket = self.config["bucket"]
        policy = self._current_policy(s3)

        baseline_path = self._baseline_path()
        if not baseline_path.exists():
            # First run: snapshot the current state as last-known-good.
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(policy))
            baseline = policy
        else:
            baseline = json.loads(baseline_path.read_text() or "null")

        drifted = policy != baseline
        public = self._is_public(policy)

        fetch_ok, content_type = True, None
        canary = self.config.get("canary_object")
        if canary:
            try:
                head = s3.head_object(Bucket=bucket, Key=canary)
                content_type = head.get("ContentType")
                expected = self.config.get("expected_content_type")
                fetch_ok = content_type == expected if expected else True
            except Exception:
                fetch_ok = False

        return HealthResult(
            adapter=self.name,
            ok=not drifted and fetch_ok,
            observed={
                "bucket": bucket,
                "policy_drifted": drifted,
                "public_access": public,
                "synthetic_fetch_ok": fetch_ok,
                "canary_content_type": content_type,
            },
        )

    @staticmethod
    def _is_public(policy: dict | None) -> bool:
        if not policy:
            return False
        for stmt in policy.get("Statement", []):
            principal = stmt.get("Principal")
            if stmt.get("Effect") == "Allow" and principal in ("*", {"AWS": "*"}):
                return True
        return False

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        if action not in self.ACTIONS:
            return self._unknown_action(action)
        try:
            return await asyncio.to_thread(self._revert_policy)
        except Exception as exc:
            return RemediationResult(ok=False, detail=str(exc))

    def _revert_policy(self) -> RemediationResult:
        baseline_path = self._baseline_path()
        if not baseline_path.exists():
            return RemediationResult(ok=False, detail="no stored baseline to revert to")
        baseline = json.loads(baseline_path.read_text() or "null")
        s3 = self._client()
        bucket = self.config["bucket"]
        if baseline is None:
            s3.delete_bucket_policy(Bucket=bucket)
        else:
            s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(baseline))
        return RemediationResult(ok=True, detail=f"bucket policy on {bucket} reverted to baseline")
