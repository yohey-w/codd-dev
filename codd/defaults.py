"""Single source of truth for CoDD default values.

All values here are chosen with the principle from
``feedback_codd_default_values_policy``: a default that frequently fails
in real use is a bug, not a default. Prefer "slow but works" over
"fast but errors" for values like timeouts; users who want tighter
limits override via env var or config.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# AI invocation
# ---------------------------------------------------------------------------

#: Default subprocess timeout (seconds) for any CoDD-orchestrated AI call.
#:
#: Set to 3600s (1 hour). Heavy reasoning models with ``reasoning_effort=
#: xhigh`` routinely take 5–20 minutes per call; the previous values of 300s
#: and 1800s caused frequent ``TimeoutExpired`` failures in self-improvement
#: experiments (cmd_465 Iteration 4). Override paths remain unchanged:
#:
#: * environment variable ``CODD_AI_TIMEOUT_SECONDS``
#: * ``codd.yaml`` key ``llm.timeout_seconds``
#: * legacy ``ai_timeout_seconds`` per-component config key
AI_TIMEOUT_SECONDS: float = 3600.0


__all__ = [
    "AI_TIMEOUT_SECONDS",
]
