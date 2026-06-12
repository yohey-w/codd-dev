"""A second flat-root module so the extractor sees a multi-module repo."""

from __future__ import annotations


def fmt_amount(cents: int) -> str:
    """Render integer cents as a currency string."""
    return f"{cents / 100:.2f}"


def parse_amount(text: str) -> int:
    """Parse a currency string back to integer cents."""
    return round(float(text) * 100)
