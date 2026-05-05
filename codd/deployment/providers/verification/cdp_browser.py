"""CDP browser verification template scaffold."""

from __future__ import annotations

import json
from typing import Any

from codd.deployment.providers import (
    VerificationResult,
    VerificationTemplate,
    register_verification_template,
)


def _runtime_value(runtime_state: Any, name: str, default: Any = None) -> Any:
    return getattr(runtime_state, name, default)


@register_verification_template("cdp_browser")
class CdpBrowser(VerificationTemplate):
    """Build CDP journey plans for a later execution phase."""

    def generate_test_command(self, runtime_state: Any, test_kind: str) -> str:
        plan = {
            "template": "cdp_browser",
            "test_kind": test_kind.lower(),
            "target": _runtime_value(runtime_state, "target", ""),
            "identifier": _runtime_value(runtime_state, "identifier", ""),
            "journey": _runtime_value(runtime_state, "journey", None),
            "steps": _runtime_value(runtime_state, "steps", []),
        }
        return json.dumps(plan, sort_keys=True)

    def execute(self, command: str) -> VerificationResult:
        raise NotImplementedError("CDP browser execution is implemented in a later phase")


__all__ = ["CdpBrowser"]
