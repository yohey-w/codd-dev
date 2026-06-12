"""Synthetic IaC-bearing fixture: a trivial HTTP-ish server module.

Generic and product-agnostic. The point of this fixture is the IaC files in
deploy/, which exercise derive_iac_nfrs deterministically.
"""

from __future__ import annotations


def handle(path: str) -> tuple[int, str]:
    """Return a (status, body) pair for a request path."""
    if path == "/health":
        return 200, "ok"
    return 404, "not found"
