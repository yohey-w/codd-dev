"""Shared types for stack-layer obligation checkers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObligationFinding:
    """A single obligation violation.

    An empty list of findings means the obligation is SATISFIED; a non-empty
    list means it is VIOLATED (the gate reds at the obligation's severity).
    """

    obligation_id: str
    location: str
    detail: str
