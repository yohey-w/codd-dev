"""H8 — "local green does not prove deployed green" survives in the RUNBOOK (F-7).

The incident: a schema team stood up a throwaway local PostgreSQL, verified RLS
and the public functions end to end, and went fully green. Production still
returned nothing for every token, because `pgcrypto` lives in `public` locally
and in `extensions` on Supabase — so pinning a `security definer` function's
`search_path` to `public, pg_temp` (correct in itself) hid `digest()` on the
deployed platform only. The failure surfaced as *zero rows*, which is
indistinguishable from "no data yet" on screen.

This one is deliberately a DOCUMENT regression test, not a behavioural one.
Faking a platform difference in a temp directory would test the fake, and a
green fake is precisely the failure mode being warned about. CoDD's shipped
answer is a completion condition in RUNBOOK Phase 5, so the completion condition
is what gets pinned.

The RUNBOOK is located from the repository root derived from the imported `codd`
package — no absolute path — so this keeps working once the suite moves into
`codd-dev/tests/hostile/`.
"""

from __future__ import annotations

import re

PHASE_5 = re.compile(r"^### Phase 5\b", re.MULTILINE)
PHASE_6 = re.compile(r"^### Phase 6\b", re.MULTILINE)


def _phase_5_section(repo_root) -> str:
    text = (repo_root / "RUNBOOK.md").read_text(encoding="utf-8")
    start = PHASE_5.search(text)
    assert start, "RUNBOOK.md has no `### Phase 5` heading — the anchor moved"
    end = PHASE_6.search(text, start.end())
    return text[start.start(): end.start() if end else len(text)]


def test_phase_5_states_that_local_green_is_not_deployed_green(repo_root):
    section = _phase_5_section(repo_root)
    assert "ローカル緑" in section and "デプロイ先" in section, (
        "the F-7 warning is not in RUNBOOK Phase 5 (verification). Without it "
        "the RUNBOOK's own definition of 'verified' is satisfiable by a local "
        "run, which is what produced the incident.\n---\n" + section[:1500]
    )


def test_phase_5_makes_a_deployed_run_a_release_condition(repo_root):
    """A warning without a completion condition is a note nobody acts on."""
    section = _phase_5_section(repo_root)
    assert "リリース条件" in section, (
        "Phase 5 warns about the local/deployed gap but never turns it into a "
        "release condition — the actionable half is missing.\n---\n" + section[:1500]
    )
    assert re.search(r"デプロイ先での実検証", section), (
        "the release condition does not require an actual run against the "
        "deployment target.\n---\n" + section[:1500]
    )


def test_the_warning_lives_in_phase_5_and_not_only_elsewhere(repo_root):
    """Placement, asserted distinctly from existence.

    Deleting the Phase 5 slice must delete the warning: if the text survived
    that excision it would be shelved in some other phase (an appendix, the
    overview) and a reader deciding *"is verification done?"* would never meet
    it. Phase 5 is where the completion condition is applied, so Phase 5 is
    where it has to be — not merely somewhere in the file.
    """
    text = (repo_root / "RUNBOOK.md").read_text(encoding="utf-8")
    outside = text.replace(_phase_5_section(repo_root), "")
    for phrase in ("ローカル緑", "リリース条件"):
        assert phrase in text, f"{phrase} is absent from RUNBOOK.md entirely"
        assert phrase not in outside, (
            f"{phrase} appears outside Phase 5 as well. Harmless on its own, but "
            "check the Phase 5 copy is still the operative one — a duplicate is "
            "how a warning ends up maintained in the wrong place and the "
            "operative copy quietly drifts."
        )
