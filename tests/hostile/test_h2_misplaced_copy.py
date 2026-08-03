"""H2 — a reference copy is parked under docs/requirements/ (F-2).

`requirement_reconciliation.discover_requirement_docs` rglobs
`docs/requirements/**/*.md`. The obedient response to "ship the original spec
too" is `docs/requirements/original/` — and the moment that copy lands, every
requirement unit is counted twice. In the field this took 86 units to 172 with
no warning, and nothing on screen told the reader which number was right.

CoDD's shipped answer (v3.38.0) is documentary, not algorithmic: the parser-side
duplicate detector was deliberately 見送り because requirement documents are
discovered along three different paths, and warning in only one of them reads as
"CoDD is watching all of them". So this file pins:

  * the warning text really is emitted where a user meets it (init, template);
  * the hole is still open, as a strict-xfail on the desired behaviour, so the
    day someone closes it this suite turns green-by-surprise rather than staying
    quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.config import load_project_config
from codd.requirement_reconciliation import (
    discover_requirement_docs,
    parse_requirement_units,
)


REQ = "docs/requirements/requirements.md"
COPY = "docs/requirements/original/requirements.md"


def _unit_count(project, section: str) -> int:
    config = load_project_config(project.root)
    total = 0
    for doc in discover_requirement_docs(project.root, config):
        total += len(
            parse_requirement_units(
                doc.read_text(encoding="utf-8"), doc.name, sections=(section,)
            )
        )
    return total


def test_init_tells_the_user_that_requirements_dir_is_canon(make_project):
    """The warning must reach the user at the moment they are shown the dir."""
    project = make_project()
    out = project.init_output
    assert "docs/requirements/ is CANON" in out, out
    assert "counted twice" in out, out
    assert "docs/source/" in out, (
        "the warning names the problem but not the way out — a user told 'do not "
        "put it there' still has to put it somewhere:\n" + out
    )


def test_codd_yaml_template_repeats_the_warning_where_config_is_edited(make_project):
    """Second delivery point: the file the user opens to configure discovery."""
    project = make_project()
    text = project.config_text()          # raw: the guidance lives in comments
    assert "docs/requirements/ is CANON" in text, text
    assert "DOUBLES every unit" in text or "counted twice" in text, text


def test_a_copy_under_requirements_still_doubles_every_unit(
    make_project, requirements_section
):
    """Characterisation of the OPEN hole — the reason the warning exists.

    This test asserts the *current* (bad) behaviour on purpose. Pinning it means
    the doubling is a documented, measured fact rather than folklore, and the
    paired strict-xfail below is what flips when it is fixed.
    """
    project = make_project()
    baseline = _unit_count(project, requirements_section)
    assert baseline > 0, "fixture parsed to zero units — the section filter is wrong"

    Path(project.root / COPY).parent.mkdir(parents=True, exist_ok=True)
    (project.root / COPY).write_text(project.read(REQ), encoding="utf-8")

    assert _unit_count(project, requirements_section) == baseline * 2, (
        "the doubling did not reproduce — discovery may have changed; re-check "
        "whether the warning texts above are still describing reality"
    )


@pytest.mark.xfail(
    strict=True,
    reason="F-2 parser-side duplicate detection was deliberately deferred "
           "(three discovery paths; a warning in one reads as coverage of all). "
           "Strict-xfail so this reports the day the hole is closed.",
)
def test_discovery_flags_two_documents_with_identical_content(make_project):
    """Desired behaviour: identical bytes discovered twice is machine-detectable."""
    project = make_project()
    Path(project.root / COPY).parent.mkdir(parents=True, exist_ok=True)
    (project.root / COPY).write_text(project.read(REQ), encoding="utf-8")

    config = load_project_config(project.root)
    docs = discover_requirement_docs(project.root, config)
    digests = {Path(d).read_bytes() for d in docs}
    assert len(docs) == len(digests), (
        "two discovered requirement documents have identical content and CoDD "
        "counted both"
    )


def test_canon_ledger_surfaces_the_stray_copy(make_project, canon_mechanism):
    """Partial mitigation that DOES exist today: the copy shows as not-in-ledger.

    Amber, not red, by design — adding a file is already visible in `git status`.
    But it is the one place the stray copy becomes visible without reading the
    unit count, so pin it.
    """
    project = make_project()
    Path(project.root / COPY).parent.mkdir(parents=True, exist_ok=True)
    (project.root / COPY).write_text(project.read(REQ), encoding="utf-8")

    if not canon_mechanism:
        # No ledger: assert the state of the world this branch describes, i.e.
        # nothing at all surfaces the copy. Not a no-op — it fails if a ledger
        # appears without the probe noticing.
        assert not project.exists("codd/canon.lock")
        return

    status = project.codd("canon", "status")
    assert "original/requirements.md" in status.output, (
        "the stray copy under docs/requirements/ is invisible to canon status:\n"
        + status.output
    )
