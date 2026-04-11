"""E2E test runner with automatic setup and CI/CDP separation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from codd.config import load_project_config


def run_e2e(
    path: str = ".",
    deploy: bool = False,
    base_url: str | None = None,
) -> int:
    """Run E2E tests with automatic prerequisite setup.

    1. Load verify config from codd.yaml
    2. Run e2e_setup_command if configured (e.g. playwright install)
    3. Run the appropriate e2e command (ci or deploy)
    4. Return exit code
    """
    project_root = Path(path).resolve()
    config = load_project_config(project_root)
    verify_config = config.get("verify", {})

    # Determine which command to run
    if deploy:
        cmd = verify_config.get("e2e_deploy_command")
        label = "deploy (CDP-only)"
    else:
        cmd = verify_config.get("e2e_ci_command") or verify_config.get("e2e_command")
        label = "CI"

    if not cmd:
        print(f"Error: No e2e {'deploy' if deploy else 'ci'} command configured in codd.yaml verify section.")
        return 1

    # Step 1: Run setup command if configured
    setup_cmd = verify_config.get("e2e_setup_command")
    if setup_cmd:
        print(f"[codd verify] Running E2E setup: {setup_cmd}")
        setup_result = subprocess.run(
            setup_cmd,
            shell=True,
            cwd=project_root,
            env=_build_env(base_url),
        )
        if setup_result.returncode != 0:
            print(f"Error: E2E setup command failed (exit {setup_result.returncode})")
            return setup_result.returncode

    # Step 2: Run E2E tests
    print(f"[codd verify] Running {label} E2E tests: {cmd}")
    env = _build_env(base_url)
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=project_root,
        env=env,
    )

    if result.returncode == 0:
        print(f"[codd verify] {label} E2E tests passed.")
    else:
        print(f"[codd verify] {label} E2E tests failed (exit {result.returncode}).")

    return result.returncode


def _build_env(base_url: str | None) -> dict[str, str]:
    """Build environment dict with optional BASE_URL override."""
    env = os.environ.copy()
    if base_url:
        env["BASE_URL"] = base_url
    return env
