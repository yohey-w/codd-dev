"""Path-escape jail coverage for ``operations_derive`` requirement-doc reads.

``uncovered_requirement_units`` reads requirement documents via
``discover_requirement_docs`` → ``_read_doc_texts`` → ``Path.read_text`` and
folds their contents into the derivation evidence (the units proposed as new
``operation_flow`` entries). A requirement-doc path that is absolute, ``../``
traversal, or an in-root symlink whose target escapes the project root must NOT
be read — otherwise an out-of-root document drives operation derivation.

``discover_requirement_docs`` already jails its output; ``_read_doc_texts``
re-confines defensively in this module's own layer. These tests pin the escape
at the module boundary (``_read_doc_texts``) and end-to-end
(``uncovered_requirement_units``), plus in-root regressions (anti-false-red).
``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import codd.operations_derive as opx
from codd.operations_derive import _read_doc_texts


# A behaviour string that, if read, would surface as an uncovered requirement
# unit (it shares no token with the declared operation below).
SECRET_BEHAVIOUR = "Teleport quux artifacts offsite"


def _seed_outside_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    doc = outside / "secret_req.md"
    doc.write_text(
        "## Functional requirements\n\n"
        "| Behaviour | Note |\n"
        "| --- | --- |\n"
        f"| {SECRET_BEHAVIOUR} | leaked |\n",
        encoding="utf-8",
    )
    return doc


def _write_codd_yaml(root: Path, config: dict) -> None:
    codd_dir = root / "codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


# --- _read_doc_texts re-confine (module boundary) -----------------------------


def test_read_doc_texts_drops_out_of_root_path(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    secret = _seed_outside_doc(tmp_path)
    in_root = root / "real.md"
    in_root.write_text("# real in-root\n", encoding="utf-8")

    texts = _read_doc_texts([in_root, secret], root)

    sources = {src for src, _ in texts}
    assert str(in_root) in sources, "in-root doc dropped (false-red)"
    assert str(secret) not in sources, (
        "out-of-root requirement doc was read into derivation evidence"
    )


def test_read_doc_texts_drops_in_root_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    secret = _seed_outside_doc(tmp_path)
    link = root / "leak.md"
    link.symlink_to(secret)

    texts = _read_doc_texts([link], root)

    assert texts == [], (
        "in-root symlink escaping the root was read into derivation evidence"
    )


# --- end-to-end: uncovered_requirement_units ----------------------------------


def test_configured_out_of_root_doc_not_derived(tmp_path):
    """A unit that exists ONLY in an out-of-root configured requirement doc must
    not surface as an uncovered unit — the doc is never read."""
    root = tmp_path / "project"
    root.mkdir()
    secret = _seed_outside_doc(tmp_path)
    config = {
        "operation_flow": {
            "operations": [{"id": "list_records", "verb": "list", "target": "record"}]
        },
        "requirement_reconciliation": {
            "enabled": True,
            "sections": ["functional"],
            "docs": [str(secret)],
        },
    }
    _write_codd_yaml(root, config)

    uncovered = opx.uncovered_requirement_units(root, config)

    assert not any(
        SECRET_BEHAVIOUR in item.unit.text for item in uncovered
    ), "out-of-root requirement doc drove operation derivation"


def test_in_root_doc_still_derived(tmp_path):
    """Anti-false-red: an in-root requirement doc still yields uncovered units."""
    root = tmp_path / "project"
    root.mkdir()
    req_dir = root / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "requirements.md").write_text(
        "## Functional requirements\n\n"
        "| Behaviour | Note |\n"
        "| --- | --- |\n"
        f"| {SECRET_BEHAVIOUR} | in-root |\n",
        encoding="utf-8",
    )
    config = {
        "operation_flow": {
            "operations": [{"id": "list_records", "verb": "list", "target": "record"}]
        },
        "requirement_reconciliation": {"enabled": True, "sections": ["functional"]},
    }
    _write_codd_yaml(root, config)

    uncovered = opx.uncovered_requirement_units(root, config)

    assert any(
        SECRET_BEHAVIOUR in item.unit.text for item in uncovered
    ), "in-root requirement unit was not derived (false-red)"
