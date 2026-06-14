"""Provider interfaces for deployment verification."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SchemaProvider(ABC):
    @abstractmethod
    def extract_schema(self, project_root: Path) -> dict:
        """Extract database schema metadata from ``project_root``."""

    @abstractmethod
    def detect_seed_files(self, project_root: Path) -> list[Path]:
        """Return seed files that may produce runtime seed state."""

    @abstractmethod
    def detect_migrations(self, project_root: Path) -> list[Path]:
        """Return migration files that may produce runtime schema state."""


SCHEMA_PROVIDERS: dict[str, type[SchemaProvider]] = {}


def register_schema_provider(name: str):
    """Register a schema provider class under ``name``."""

    def decorator(cls):
        SCHEMA_PROVIDERS[name] = cls
        return cls

    return decorator


class DeployTarget(ABC):
    @abstractmethod
    def parse_deploy_yaml(self, deploy_yaml: dict) -> list:
        """Parse target-specific deployment steps."""

    @abstractmethod
    def infer_executes_in_order(self, deployment_doc) -> list:
        """Infer ordered execution edges from a deployment document."""

    @abstractmethod
    def get_post_deploy_hooks(self) -> list[str]:
        """Return post-deploy verification hooks."""


DEPLOY_TARGETS: dict[str, type[DeployTarget]] = {}


def register_deploy_target(name: str):
    """Register a deploy target class under ``name``."""

    def decorator(cls):
        DEPLOY_TARGETS[name] = cls
        return cls

    return decorator


class VerificationTemplate(ABC):
    @abstractmethod
    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        """Generate the command used to verify ``runtime_state``."""

    @abstractmethod
    def execute(self, command: str, cwd: Path | None = None) -> "VerificationResult":
        """Execute a verification command and return its result.

        ``cwd`` is the project root the command must run in. A test runner like
        ``vitest`` roots its config/collection at the process working directory,
        so executing it from the orchestrator's cwd (e.g. the CoDD install tree)
        makes it load the WRONG ``vitest.config.ts`` and collect 0 tests. Pass
        the generated project root here so every runner roots at the project.
        ``None`` keeps the caller's current working directory (legacy behaviour).
        """


VERIFICATION_TEMPLATES: dict[str, type[VerificationTemplate]] = {}


def register_verification_template(name: str):
    """Register a verification template class under ``name``."""

    def decorator(cls):
        VERIFICATION_TEMPLATES[name] = cls
        return cls

    return decorator


class VerificationResult:
    def __init__(self, passed: bool, output: str = "", duration: float = 0.0):
        self.passed = passed
        self.output = output
        self.duration = duration


__all__ = [
    "DEPLOY_TARGETS",
    "SCHEMA_PROVIDERS",
    "VERIFICATION_TEMPLATES",
    "DeployTarget",
    "SchemaProvider",
    "VerificationResult",
    "VerificationTemplate",
    "register_deploy_target",
    "register_schema_provider",
    "register_verification_template",
]
