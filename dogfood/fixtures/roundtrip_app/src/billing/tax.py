"""Tax calculation for the round-trip fixture."""

from __future__ import annotations


def with_tax(cents: int, rate_bps: int) -> int:
    """Add tax expressed in basis points (1% == 100 bps)."""
    return cents + (cents * rate_bps // 10_000)
