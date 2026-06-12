"""Flat-layout synthetic fixture: a tiny ledger app, all modules at repo root.

Generic and project-agnostic on purpose (Generality Gate): no real product
names. Exercises the deterministic extractor on a flat (no src/) Python repo.
"""

from __future__ import annotations


class Account:
    """An in-memory account with a running balance."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.balance = 0

    def deposit(self, amount: int) -> int:
        self.balance += amount
        return self.balance

    def withdraw(self, amount: int) -> int:
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount
        return self.balance


def transfer(src: Account, dst: Account, amount: int) -> None:
    """Move ``amount`` from ``src`` to ``dst``."""
    src.withdraw(amount)
    dst.deposit(amount)
