"""Shared JSON serialization defaults for CoDD.

Design-doc frontmatter and impact/event payloads can carry values the stdlib
``json`` encoder rejects — most commonly a bare ``date: 2026-05-29`` which PyYAML
parses to a :class:`datetime.date` (issue #28). Every ``json.dumps`` that may
serialize frontmatter-derived or impact/event data must pass
``default=json_default`` so serialization never raises.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any


def json_default(obj: Any) -> Any:
    """Fallback for ``json.dumps(default=...)``.

    Coerce date/datetime to ISO 8601 and any other non-serializable value to its
    string form so serialization never raises. Generic by design: not tied to any
    field name, project, or code path.
    """
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)


def dumps(obj: Any, **kwargs: Any) -> str:
    """``json.dumps`` with :func:`json_default` pre-wired (override via kwargs)."""
    kwargs.setdefault("default", json_default)
    return json.dumps(obj, **kwargs)
