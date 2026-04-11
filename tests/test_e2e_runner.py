"""Tests for E2E runner with automatic setup."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codd.e2e_runner import run_e2e, _build_env


class TestBuildEnv:
    def test_includes_base_url_when_provided(self):
        env = _build_env("http://example.com:3000")
        assert env["BASE_URL"] == "http://example.com:3000"

    def test_no_base_url_when_none(self):
        env = _build_env(None)
        assert "BASE_URL" not in env or env.get("BASE_URL") == ""


class TestRunE2e:
    @patch("codd.e2e_runner.subprocess.run")
    @patch("codd.e2e_runner.load_project_config")
    def test_runs_setup_then_ci_command(self, mock_config, mock_run):
        mock_config.return_value = {
            "verify": {
                "e2e_setup_command": "npx playwright install chromium",
                "e2e_ci_command": "npx playwright test --grep-invert @cdp-only",
            }
        }
        mock_run.return_value = MagicMock(returncode=0)

        result = run_e2e(path="/tmp/fake", deploy=False)

        assert result == 0
        assert mock_run.call_count == 2
        # First call = setup
        assert "playwright install" in mock_run.call_args_list[0][0][0]
        # Second call = test
        assert "grep-invert" in mock_run.call_args_list[1][0][0]

    @patch("codd.e2e_runner.subprocess.run")
    @patch("codd.e2e_runner.load_project_config")
    def test_runs_deploy_command(self, mock_config, mock_run):
        mock_config.return_value = {
            "verify": {
                "e2e_setup_command": "npx playwright install chromium",
                "e2e_deploy_command": "PLAYWRIGHT_DISABLE_WEB_SERVER=1 npx playwright test --grep @cdp-only",
            }
        }
        mock_run.return_value = MagicMock(returncode=0)

        result = run_e2e(path="/tmp/fake", deploy=True, base_url="http://vps:3000")

        assert result == 0
        assert mock_run.call_count == 2
        # Second call = deploy command
        assert "@cdp-only" in mock_run.call_args_list[1][0][0]
        # BASE_URL should be in env
        env = mock_run.call_args_list[1][1]["env"]
        assert env["BASE_URL"] == "http://vps:3000"

    @patch("codd.e2e_runner.subprocess.run")
    @patch("codd.e2e_runner.load_project_config")
    def test_skips_setup_when_not_configured(self, mock_config, mock_run):
        mock_config.return_value = {
            "verify": {
                "e2e_ci_command": "npx playwright test",
            }
        }
        mock_run.return_value = MagicMock(returncode=0)

        result = run_e2e(path="/tmp/fake")

        assert result == 0
        assert mock_run.call_count == 1  # Only test, no setup

    @patch("codd.e2e_runner.load_project_config")
    def test_returns_error_when_no_command(self, mock_config):
        mock_config.return_value = {"verify": {}}

        result = run_e2e(path="/tmp/fake")

        assert result == 1

    @patch("codd.e2e_runner.subprocess.run")
    @patch("codd.e2e_runner.load_project_config")
    def test_stops_on_setup_failure(self, mock_config, mock_run):
        mock_config.return_value = {
            "verify": {
                "e2e_setup_command": "npx playwright install chromium",
                "e2e_ci_command": "npx playwright test",
            }
        }
        mock_run.return_value = MagicMock(returncode=1)

        result = run_e2e(path="/tmp/fake")

        assert result == 1
        assert mock_run.call_count == 1  # Stopped at setup, didn't run tests
