from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from codd.deploy_targets import register_target
from codd.deploy_targets.base import DeployTarget


_ENV_REF = re.compile(r"\$\{env:([^}]+)\}")


@register_target("app_service")
class AppServiceTarget(DeployTarget):
    """Deploy target for Azure App Service via the az CLI."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.subscription_id = _expand_env(config.get("subscription_id", ""))
        self.resource_group = _expand_env(config["resource_group"])
        self.app_name = _expand_env(config["app_name"])
        self.package_path = _expand_env(config.get("package_path", "."))
        self.slot = _expand_env(config.get("slot", ""))

    def _run_az(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run an az CLI command."""
        command = ["az", *args]
        if self.subscription_id:
            command.extend(["--subscription", self.subscription_id])
        return subprocess.run(command, capture_output=True, text=True, check=check)

    def snapshot(self) -> dict[str, Any]:
        """Capture current App Service metadata for deployment logs."""
        args = [
            "webapp",
            "show",
            "--resource-group",
            self.resource_group,
            "--name",
            self.app_name,
            "--output",
            "json",
        ]
        if self.slot:
            args.extend(["--slot", self.slot])

        result = self._run_az(*args, check=False)
        snapshot = {
            "app_name": self.app_name,
            "resource_group": self.resource_group,
        }
        if self.slot:
            snapshot["slot"] = self.slot
        if result.returncode != 0:
            return snapshot

        try:
            info = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return snapshot

        snapshot.update(
            {
                "default_host_name": info.get("defaultHostName"),
                "state": info.get("state"),
                "last_modified_time_utc": info.get("lastModifiedTimeUtc"),
            }
        )
        return snapshot

    def dry_run(self) -> list[str]:
        """Return proposed Azure actions without calling az."""
        slot_flag = f" --slot {self.slot}" if self.slot else ""
        subscription_flag = f" --subscription {self.subscription_id}" if self.subscription_id else ""
        return [
            f"[az] Check auth: az account show{subscription_flag}",
            (
                "[az] Snapshot: az webapp show "
                f"--resource-group {self.resource_group} --name {self.app_name}{slot_flag}"
            ),
            (
                "[az] Deploy: az webapp deploy "
                f"--resource-group {self.resource_group} --name {self.app_name}{slot_flag} "
                f"--src-path {self.package_path} --type zip"
            ),
            "[healthcheck] Wait for service ready",
        ]

    def deploy(self) -> bool:
        """Deploy a zip package to Azure App Service."""
        args = [
            "webapp",
            "deploy",
            "--resource-group",
            self.resource_group,
            "--name",
            self.app_name,
            "--src-path",
            self.package_path,
            "--type",
            "zip",
        ]
        if self.slot:
            args.extend(["--slot", self.slot])

        try:
            self._run_az(*args)
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Azure App Service deploy failed: {exc.stderr or exc}")
            return False

    def healthcheck(self) -> bool:
        """Delegate optional HTTP healthcheck to the deployer helper."""
        healthcheck_config = self.config.get("healthcheck", {})
        if not healthcheck_config:
            return True
        from codd.deployer import run_healthcheck

        return run_healthcheck(
            url=str(healthcheck_config["url"]),
            expected_status=int(healthcheck_config.get("expected_status", 200)),
            timeout_seconds=int(healthcheck_config.get("timeout_seconds", 90)),
            retries=int(healthcheck_config.get("retries", 3)),
        )

    def rollback(self, snapshot: dict[str, Any]) -> bool:
        """Minimal rollback hook: restart the App Service."""
        args = [
            "webapp",
            "restart",
            "--resource-group",
            self.resource_group,
            "--name",
            self.app_name,
        ]
        if self.slot or snapshot.get("slot"):
            args.extend(["--slot", self.slot or str(snapshot["slot"])])

        try:
            self._run_az(*args)
            print(f"Restarted {self.app_name} for rollback")
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Azure App Service rollback failed: {exc.stderr or exc}")
            return False


def _expand_env(value: Any) -> str:
    text = str(value)
    return _ENV_REF.sub(lambda match: os.environ.get(match.group(1), match.group(0)), text)
