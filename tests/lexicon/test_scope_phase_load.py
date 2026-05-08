from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from codd.lexicon import LexiconError, load_lexicon, validate_lexicon


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": "1.0",
        "node_vocabulary": [{"id": "entity", "description": "Domain entity"}],
        "naming_conventions": [],
        "design_principles": [],
    }
    payload.update(overrides)
    return payload


def _write(root: Path, payload: dict[str, Any]) -> None:
    (root / "project_lexicon.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_load_lexicon_reads_scope_and_phase(tmp_path: Path) -> None:
    _write(tmp_path, _payload(scope="system_implementation", phase="mvp"))

    lexicon = load_lexicon(tmp_path)

    assert lexicon is not None
    assert lexicon.scope == "system_implementation"
    assert lexicon.phase == "mvp"


def test_load_lexicon_defaults_scope_system_implementation_phase_production(
    tmp_path: Path,
) -> None:
    """cmd_455: undeclared scope defaults to system_implementation.

    Business-tier dimensions (goal/KPI, acceptance/UAT, risk register) are
    suppressed unless the project opts in with `scope: full` or
    `scope: business_only`.
    """

    _write(tmp_path, _payload())

    lexicon = load_lexicon(tmp_path)

    assert lexicon is not None
    assert lexicon.scope == "system_implementation"
    assert lexicon.phase == "production"


def test_scope_and_phase_must_be_strings() -> None:
    with pytest.raises(LexiconError, match="scope must be a string"):
        validate_lexicon(_payload(scope=["system_implementation"]))

    with pytest.raises(LexiconError, match="phase must be a string"):
        validate_lexicon(_payload(phase={"name": "mvp"}))
