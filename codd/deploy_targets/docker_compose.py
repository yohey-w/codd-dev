from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
from typing import Any

from codd.deploy_targets import register_target
from codd.deploy_targets.base import DeployTarget


@register_target("docker_compose")
class DockerComposeTarget(DeployTarget):
    """Deploy target for Docker Compose over SSH."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.host = str(config["host"])
        self.ssh_user = str(config.get("ssh_user", "root"))
        self.ssh_key = Path(str(config["ssh_key"])).expanduser()
        self.working_dir = str(config.get("working_dir", "/opt/app"))
        self.compose_file = str(config.get("compose_file", "docker-compose.production.yml"))
        self.git_branch = str(config.get("git_branch", "main"))
        self.git_remote = str(config.get("git_remote", "origin"))

    def _run_ssh(self, command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a command on the remote host via ssh."""
        ssh_cmd = [
            "ssh",
            "-i",
            str(self.ssh_key),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=30",
            f"{self.ssh_user}@{self.host}",
            command,
        ]
        return subprocess.run(ssh_cmd, capture_output=True, text=True, check=check)

    def snapshot(self) -> dict[str, Any]:
        """Capture current git HEAD on the remote host."""
        result = self._run_ssh(f"cd {self._q(self.working_dir)} && git rev-parse HEAD")
        return {
            "git_commit": result.stdout.strip(),
            "host": self.host,
            "working_dir": self.working_dir,
        }

    def dry_run(self) -> list[str]:
        """Return proposed actions without opening an SSH connection."""
        return [
            f"[ssh] {self.ssh_user}@{self.host}: cd {self.working_dir}",
            f"[ssh] git fetch {self.git_remote} && git checkout {self.git_branch} && git pull {self.git_remote} {self.git_branch}",
            f"[ssh] docker compose -f {self.compose_file} pull",
            f"[ssh] docker compose -f {self.compose_file} up -d",
            "[healthcheck] Wait for service ready",
        ]

    def deploy(self) -> bool:
        """Execute git pull and docker compose update."""
        try:
            pull_cmd = (
                f"cd {self._q(self.working_dir)} && "
                f"git fetch {self._q(self.git_remote)} && "
                f"git checkout {self._q(self.git_branch)} && "
                f"git pull {self._q(self.git_remote)} {self._q(self.git_branch)}"
            )
            self._run_ssh(pull_cmd)
            compose_cmd = (
                f"cd {self._q(self.working_dir)} && "
                f"docker compose -f {self._q(self.compose_file)} pull && "
                f"docker compose -f {self._q(self.compose_file)} up -d"
            )
            self._run_ssh(compose_cmd)
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Deploy failed: {exc.stderr or exc}")
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
            timeout_seconds=int(healthcheck_config.get("timeout_seconds", 60)),
            retries=int(healthcheck_config.get("retries", 3)),
        )

    def rollback(self, snapshot: dict[str, Any]) -> bool:
        """Rollback to a captured git commit."""
        commit = snapshot.get("git_commit")
        if not commit:
            print("No commit hash in snapshot, cannot rollback")
            return False
        try:
            rollback_cmd = (
                f"cd {self._q(self.working_dir)} && "
                f"git checkout {self._q(str(commit))} && "
                f"docker compose -f {self._q(self.compose_file)} up -d"
            )
            self._run_ssh(rollback_cmd)
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Rollback failed: {exc.stderr or exc}")
            return False

    @staticmethod
    def _q(value: str) -> str:
        return shlex.quote(value)
