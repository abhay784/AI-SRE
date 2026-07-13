"""Silent-backup-failure adapter (PRD failure #10).

Detection: verify the latest backup actually *restores*, not just that the
job ran — freshness check + `pg_restore --list` structural validation (or a
full test-restore into a scratch database if scratch_dsn is configured).

Remediation: none in real time; this is a "catch it before you need it"
alert, so ACTIONS is empty and the rule layer escalates.

Config:
  backup_dir: directory containing pg_dump custom-format backups (*.dump)
  scratch_dsn: optional — restore target for a full test-restore
  max_age_hours: freshness threshold (default 26)
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from ..core.adapter import BaseAdapter
from ..core.models import HealthResult, RemediationResult


class BackupAdapter(BaseAdapter):
    name = "backup"
    default_interval = 21600.0  # every 6 hours
    ACTIONS: dict[str, str] = {}  # alert-only failure mode

    async def check_health(self) -> HealthResult:
        try:
            return await asyncio.to_thread(self._verify)
        except Exception as exc:
            return HealthResult(adapter=self.name, ok=False, error=str(exc))

    def _verify(self) -> HealthResult:
        backup_dir = Path(self.config["backup_dir"])
        dumps = sorted(backup_dir.glob("*.dump"), key=lambda p: p.stat().st_mtime)
        if not dumps:
            return HealthResult(adapter=self.name, ok=False,
                                observed={"backup_age_hours": None, "test_restore_ok": False,
                                          "detail": "no backups found"})
        latest = dumps[-1]
        age_hours = (time.time() - latest.stat().st_mtime) / 3600

        restore_ok, detail = self._test_restore(latest)
        return HealthResult(
            adapter=self.name,
            ok=restore_ok and age_hours <= float(self.config.get("max_age_hours", 26)),
            observed={
                "latest_backup": latest.name,
                "backup_age_hours": round(age_hours, 1),
                "test_restore_ok": restore_ok,
                "detail": detail,
            },
        )

    def _test_restore(self, dump: Path) -> tuple[bool, str]:
        # Structural validation: can pg_restore parse the archive's TOC?
        proc = subprocess.run(["pg_restore", "--list", str(dump)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return False, f"pg_restore --list failed: {proc.stderr.strip()[:300]}"
        if scratch := self.config.get("scratch_dsn"):
            proc = subprocess.run(
                ["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", scratch, str(dump)],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                return False, f"test restore failed: {proc.stderr.strip()[:300]}"
            return True, "full test-restore into scratch db succeeded"
        return True, "archive TOC valid (configure scratch_dsn for full test-restore)"

    async def remediate(self, action: str, params: dict) -> RemediationResult:
        return self._unknown_action(action)
