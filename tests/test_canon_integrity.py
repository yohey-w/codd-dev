"""Falsification tests for the canon integrity tripwire (v3.39.0).

The accident being fixed has no other trigger: a requirement unit is a Markdown
table ROW, so a formatter that only re-aligns table pipes changes no meaning —
review passes, every other CoDD check stays green — while byte-identity with the
human-approved original is destroyed. Observed in the field: ``npx prettier
--write .`` rewrote 440 lines of a requirements table.

These tests hold the four properties the mechanism has to have, each stated so
that removing the feature makes it fail:

1. one byte of change in a canon document makes ``codd dag verify`` red;
2. after ``codd canon accept`` it is green again — and accept is the ONLY writer
   (verify must never refresh the ledger by itself, or it detects nothing);
3. a document excluded via ``scan.exclude`` is not canon (v3.38.0 F-8 symmetry);
4. an existing project with no ledger is guided, not broken — amber WARN, never
   red, and the pre-commit hook does not block it.

Plus the negative controls that keep the gate honest: an untracked in-scope
document is amber (an addition is visible in git; hard-failing it would make
every greenfield run red at the first requirements file), a deleted canon
document is red, and the check is selected by the pinned project-type profiles
rather than silently unselected by ``dag.enabled_checks``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.canon import (
    CANON_LOCK_NAME,
    LEDGER_MAGIC,
    LEDGER_VERSION,
    LedgerEntry,
    canon_documents,
    canon_lock_path,
    canon_settings,
    compute_digests,
    evaluate_canon,
    ledger_digests,
    load_ledger,
    normalize_accept_reference,
    write_ledger,
)


REQUIREMENTS_TABLE = """---
codd:
  node_id: req:system
  type: requirement
---

# Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-1 | The system SHALL do the thing | planned |
| FR-2 | The system SHALL do the other thing | planned |
"""

# The same table, pipes re-aligned exactly as a Markdown formatter would. No
# meaning changes; only the bytes do. This is the observed accident, in miniature.
REQUIREMENTS_TABLE_PRETTIFIED = """---
codd:
  node_id: req:system
  type: requirement
---

# Requirements

| ID   | Requirement                         | Status  |
| ---- | ----------------------------------- | ------- |
| FR-1 | The system SHALL do the thing       | planned |
| FR-2 | The system SHALL do the other thing | planned |
"""


def _project(tmp_path: Path, config_extra: dict | None = None) -> Path:
    """A minimal CoDD project with one requirement document."""
    root = tmp_path / "proj"
    (root / "codd").mkdir(parents=True)
    (root / "docs" / "requirements").mkdir(parents=True)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE, encoding="utf-8"
    )
    config: dict = {
        "project": {"name": "p", "language": "python"},
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/"], "exclude": []},
    }
    if config_extra:
        config.update(config_extra)
    (root / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    return root


def _config(root: Path) -> dict:
    from codd.config import load_project_config

    return load_project_config(root)


def _accept(root: Path, reference: str = "test-ref") -> Path:
    lock_path = canon_lock_path(root)
    assert lock_path is not None
    payload = None
    try:
        payload = load_ledger(lock_path)
    except ValueError:
        payload = None
    previous = (
        {entry.path: entry for entry in payload.get("entries", [])} if payload else {}
    )
    write_ledger(
        lock_path,
        compute_digests(root, config=_config(root)),
        accepted_for=reference,
        previous=previous,
    )
    return lock_path


def _check(root: Path):
    from codd.dag.checks.canon_integrity import CanonIntegrityCheck

    return CanonIntegrityCheck(None, root, {}).run(codd_config=_config(root))


# ── registration / selection ────────────────────────────────────────────────


def test_check_is_registered():
    from codd.dag.checks import get_registry
    from codd.dag.checks.canon_integrity import CanonIntegrityCheck
    from codd.dag.runner import CHECK_MODULES, _ensure_checks_registered

    assert "codd.dag.checks.canon_integrity" in CHECK_MODULES
    _ensure_checks_registered()
    assert get_registry()["canon_integrity"] is CanonIntegrityCheck


@pytest.mark.parametrize("profile", ["web", "cli", "mobile", "iot"])
def test_pinned_project_type_profiles_select_the_check(profile):
    """A profile that pins ``enabled_checks`` and omits us runs without the gate.

    ``dag.enabled_checks`` is an allowlist, so a newly shipped check is a silent
    no-op for every project on a pinned profile — precisely the invisible-damage
    signature this feature exists to remove.
    """
    import codd

    payload = yaml.safe_load(
        (Path(codd.__file__).parent / "dag" / "defaults" / f"{profile}.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "canon_integrity" in payload["enabled_checks"]


# ── (1) one byte of change → RED ────────────────────────────────────────────


def test_reformatted_table_is_red(tmp_path):
    root = _project(tmp_path)
    _accept(root)
    assert _check(root).passed is True  # baseline: accepted state is green

    doc = root / "docs" / "requirements" / "requirements.md"
    doc.write_text(REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8")

    result = _check(root)
    assert result.passed is False
    assert result.severity == "red"
    assert result.status == "fail"
    assert [v["path"] for v in result.violations] == ["docs/requirements/requirements.md"]
    assert "canon accept" in result.message


def test_single_byte_change_is_red(tmp_path):
    """Byte-identity, not semantic equality: a trailing newline counts."""
    root = _project(tmp_path)
    _accept(root)

    doc = root / "docs" / "requirements" / "requirements.md"
    doc.write_bytes(doc.read_bytes() + b"\n")

    result = _check(root)
    assert result.passed is False
    assert result.severity == "red"


def test_deleted_canon_document_is_red(tmp_path):
    root = _project(tmp_path)
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").unlink()

    result = _check(root)
    assert result.passed is False
    assert [v["type"] for v in result.violations] == ["canon_missing"]


# ── (2) accept → GREEN, and accept is the ONLY writer ───────────────────────


def test_accept_makes_it_green_again(tmp_path):
    root = _project(tmp_path)
    _accept(root)
    doc = root / "docs" / "requirements" / "requirements.md"
    doc.write_text(REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8")
    assert _check(root).passed is False

    _accept(root)

    result = _check(root)
    assert result.passed is True
    assert result.status == "pass"
    assert result.checked_count == 1


def test_verify_never_refreshes_the_ledger(tmp_path):
    """A self-updating ledger detects nothing. Running the check must not write."""
    root = _project(tmp_path)
    lock_path = _accept(root)
    before = lock_path.read_bytes()

    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    assert _check(root).passed is False
    assert _check(root).passed is False  # still red on a second run

    assert lock_path.read_bytes() == before


def test_accept_is_byte_stable_when_nothing_changed(tmp_path):
    """No timestamp / tool version in the payload: a no-op accept is a no-op diff."""
    root = _project(tmp_path)
    first = _accept(root).read_bytes()
    assert _accept(root).read_bytes() == first


# ── (3) scan.exclude is honoured (v3.38.0 F-8 symmetry) ─────────────────────


def test_excluded_document_is_not_canon(tmp_path):
    """Reference material excluded from scanning is not CoDD's canon either.

    Uses ``canon.docs`` pointing at ``docs/`` so the document would otherwise be
    in scope — the exclusion, not the discovery default, has to be what removes
    it.
    """
    root = _project(
        tmp_path,
        {
            "canon": {"docs": ["docs/"]},
            "scan": {
                "source_dirs": ["src/"],
                "doc_dirs": ["docs/"],
                "exclude": ["docs/source/**"],
            },
        },
    )
    (root / "docs" / "source").mkdir(parents=True)
    vendored = root / "docs" / "source" / "customer-spec.md"
    vendored.write_text("received spec, foreign text\n", encoding="utf-8")

    scoped = [
        p.relative_to(root).as_posix() for p in canon_documents(root, _config(root))
    ]
    assert "docs/source/customer-spec.md" not in scoped
    assert "docs/requirements/requirements.md" in scoped

    _accept(root)
    vendored.write_text("upstream revised their spec\n", encoding="utf-8")
    assert _check(root).passed is True


def test_negative_control_empty_exclude_covers_the_document(tmp_path):
    """The opt-in property: without an exclude the same document IS canon."""
    root = _project(
        tmp_path,
        {
            "canon": {"docs": ["docs/"]},
            "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/"], "exclude": []},
        },
    )
    (root / "docs" / "source").mkdir(parents=True)
    vendored = root / "docs" / "source" / "customer-spec.md"
    vendored.write_text("received spec\n", encoding="utf-8")

    _accept(root)
    vendored.write_text("changed\n", encoding="utf-8")
    assert _check(root).passed is False


# ── (4) a project with no ledger is guided, never broken ────────────────────


def test_no_ledger_is_amber_with_guidance_not_red(tmp_path):
    root = _project(tmp_path)
    assert canon_lock_path(root) is not None
    assert not canon_lock_path(root).exists()

    result = _check(root)
    assert result.passed is True
    assert result.severity == "amber"
    assert result.block_deploy is False
    assert [w["type"] for w in result.warnings] == ["ledger_absent"]
    assert "codd canon accept" in result.message


def test_no_ledger_and_no_canon_documents_is_skip(tmp_path):
    root = _project(tmp_path)
    (root / "docs" / "requirements" / "requirements.md").unlink()

    result = _check(root)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.severity == "info"
    assert result.checked_count == 0


def test_no_ledger_amber_renders_as_warn_not_pass(tmp_path):
    """It must not hide behind a green PASS row in the verify summary."""
    from codd.dag.result_status import pass_is_warn

    root = _project(tmp_path)
    assert pass_is_warn(_check(root)) is True


def test_hook_does_not_block_a_project_without_a_ledger(tmp_path):
    from codd.hooks import _canon_drift_blocks_commit

    root = _project(tmp_path)
    assert _canon_drift_blocks_commit(root, _config(root)) is False


def test_hook_blocks_a_reformatted_canon_document(tmp_path):
    from codd.hooks import _canon_drift_blocks_commit

    root = _project(tmp_path)
    _accept(root)
    assert _canon_drift_blocks_commit(root, _config(root)) is False

    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    assert _canon_drift_blocks_commit(root, _config(root)) is True


def test_hook_reads_a_raw_codd_yaml(tmp_path):
    """The hook loads codd.yaml WITHOUT merging defaults.yaml.

    Every canon default therefore has to exist in code too; if it only lived in
    defaults.yaml the hook would silently see ``canon`` as absent.
    """
    from codd.hooks import _canon_drift_blocks_commit

    root = _project(tmp_path)
    _accept(root)
    raw = yaml.safe_load((root / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert "canon" not in raw  # the fixture never writes the section

    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    assert _canon_drift_blocks_commit(root, raw) is True


# ── untracked: reported, but never red ──────────────────────────────────────


def test_untracked_document_is_amber_not_red(tmp_path):
    root = _project(tmp_path)
    _accept(root)
    (root / "docs" / "requirements" / "second.md").write_text(
        REQUIREMENTS_TABLE, encoding="utf-8"
    )

    result = _check(root)
    assert result.passed is True
    assert result.severity == "amber"
    assert result.block_deploy is False
    assert [w["path"] for w in result.warnings] == ["docs/requirements/second.md"]
    assert result.checked_count == 1


def test_untracked_does_not_hide_a_simultaneous_alteration(tmp_path):
    """Adding a file must never downgrade a real alteration to amber."""
    root = _project(tmp_path)
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    (root / "docs" / "requirements" / "second.md").write_text(
        REQUIREMENTS_TABLE, encoding="utf-8"
    )

    result = _check(root)
    assert result.passed is False
    assert result.severity == "red"


# ── ledger robustness ───────────────────────────────────────────────────────


def test_corrupt_ledger_is_red_not_a_silent_pass(tmp_path):
    root = _project(tmp_path)
    lock_path = _accept(root)
    lock_path.write_text("{not json", encoding="utf-8")

    result = _check(root)
    assert result.passed is False
    assert [v["type"] for v in result.violations] == ["ledger_unreadable"]


def test_future_ledger_version_is_not_reinterpreted(tmp_path):
    root = _project(tmp_path)
    lock_path = _accept(root)
    text = lock_path.read_text(encoding="utf-8")
    lock_path.write_text(
        text.replace(
            f"{LEDGER_MAGIC}{LEDGER_VERSION}", f"{LEDGER_MAGIC}{LEDGER_VERSION + 1}", 1
        ),
        encoding="utf-8",
    )

    result = _check(root)
    assert result.passed is False
    assert "unsupported ledger version" in result.violations[0]["detail"]


def test_a_v1_json_ledger_is_still_read(tmp_path):
    """Upgrading must not reset an early adopter to the "no ledger" advisory."""
    root = _project(tmp_path)
    lock_path = canon_lock_path(root)
    digests = compute_digests(root, config=_config(root))
    lock_path.write_text(
        json.dumps(
            {"version": 1, "algorithm": "sha256", "note": "n", "documents": digests},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _check(root)
    assert result.passed is True
    assert result.checked_count == 1

    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    assert _check(root).passed is False


def test_ledger_shape(tmp_path):
    root = _project(tmp_path)
    lock_path = _accept(root, "cmd_042")
    payload = load_ledger(lock_path)
    assert payload["version"] == LEDGER_VERSION
    entries = payload["entries"]
    assert [entry.path for entry in entries] == ["docs/requirements/requirements.md"]
    assert entries[0].digest.startswith("sha256:")
    assert entries[0].accepted_for == "cmd_042"

    text = lock_path.read_text(encoding="utf-8")
    assert text.startswith(f"{LEDGER_MAGIC}{LEDGER_VERSION}")
    # One line per document; the header is comments only.
    record_lines = [
        line for line in text.splitlines() if line and not line.startswith("#")
    ]
    assert record_lines == [
        "docs/requirements/requirements.md\t"
        f"{entries[0].digest}\tcmd_042"
    ]
    # The honest-limits statement travels with the file itself.
    assert "NOT AN AUTHORIZATION GATE" in text


def test_disabled_canon_is_a_skip(tmp_path):
    root = _project(tmp_path, {"canon": {"enabled": False}})
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )

    result = _check(root)
    assert result.skipped is True
    assert result.severity == "info"


def test_symlink_escaping_the_root_is_not_ledgered(tmp_path):
    """``canon.docs`` is user-controllable and rglob follows symlinks."""
    outside = tmp_path / "outside.md"
    outside.write_text("not ours\n", encoding="utf-8")
    root = _project(tmp_path, {"canon": {"docs": ["docs/"]}})
    try:
        (root / "docs" / "requirements" / "linked.md").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        pytest.skip("symlinks unavailable")

    scoped = [p.name for p in canon_documents(root, _config(root))]
    assert "linked.md" not in scoped


def test_canon_settings_defaults_match_defaults_yaml():
    """The hook's in-code defaults and defaults.yaml must not drift apart."""
    import codd

    defaults = yaml.safe_load(
        (Path(codd.__file__).parent / "defaults.yaml").read_text(encoding="utf-8")
    )
    section = defaults["canon"]
    resolved = canon_settings({})
    assert resolved.enabled is section["enabled"]
    assert list(resolved.docs) == section["docs"]
    assert resolved.severity == section["severity"]


# ── init seeds the ledger ───────────────────────────────────────────────────


def test_init_writes_a_ledger(tmp_path):
    from click.testing import CliRunner

    from codd.cli import main

    dest = tmp_path / "fresh"
    dest.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text(REQUIREMENTS_TABLE, encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "init",
            "demo",
            "--language",
            "python",
            "--dest",
            str(dest),
            "--requirements",
            str(spec),
            "--no-suggest-lexicons",
        ],
    )
    assert result.exit_code == 0, result.output

    lock_path = dest / "codd" / CANON_LOCK_NAME
    assert lock_path.is_file()
    payload = load_ledger(lock_path)
    assert payload["entries"], "the imported requirements document must be recorded"
    assert payload["entries"][0].accepted_for == "codd-init"

    status = evaluate_canon(dest, _config(dest))
    assert status.ledger_present is True
    assert status.clean is True


def test_init_lock_is_not_gitignored_by_the_shipped_template(tmp_path):
    """The ledger is worthless if the scaffold ignores it."""
    import codd

    ignored = (
        Path(codd.__file__).parent / "templates" / "gitignore.tmpl"
    ).read_text(encoding="utf-8")
    assert CANON_LOCK_NAME not in ignored
    assert "*.lock" not in ignored


# ═══════════════════════════════════════════════════════════════════════════
# Reflexive-acceptance friction (`accept` must be a decision, not a reflex)
# ═══════════════════════════════════════════════════════════════════════════


def _cli(args: list[str]):
    from click.testing import CliRunner

    from codd.cli import main

    return CliRunner().invoke(main, args)


def test_accept_without_a_work_reference_is_refused(tmp_path):
    """The argument-less reflex accept must be structurally impossible."""
    root = _project(tmp_path)
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    before = canon_lock_path(root).read_bytes()

    result = _cli(["canon", "accept", "--path", str(root)])

    assert result.exit_code == 2, result.output
    assert "work reference is required" in result.output
    assert canon_lock_path(root).read_bytes() == before, "the ledger must not move"
    assert _check(root).passed is False, "the check must still be red"


@pytest.mark.parametrize("reference", ["", "   ", "x", "a\tb"])
def test_contentless_work_references_are_rejected(reference):
    with pytest.raises(ValueError):
        normalize_accept_reference(reference)


def test_accept_records_the_work_reference_in_the_ledger(tmp_path):
    """The reference is the review trail: it has to survive into the committed file."""
    root = _project(tmp_path)
    result = _cli(["canon", "accept", "--path", str(root), "--for", "cmd_042"])
    assert result.exit_code == 0, result.output

    text = canon_lock_path(root).read_text(encoding="utf-8")
    assert "\tcmd_042" in text
    entries = load_ledger(canon_lock_path(root))["entries"]
    assert entries[0].accepted_for == "cmd_042"


def test_accept_prints_the_content_diff_not_just_filenames(tmp_path):
    """A reflex accept must have to scroll past the evidence of what it blesses."""
    import subprocess

    root = _project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=root,
        check=True,
    )
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )

    result = _cli(["canon", "accept", "--path", str(root), "--for", "cmd_042", "--dry-run"])

    assert result.exit_code == 0, result.output
    # the git --stat summary
    assert "requirements.md" in result.output
    assert "+" in result.output and "-" in result.output
    # actual patch content, not merely a filename list
    assert "| FR-1 |" in result.output
    # and the rule an agent needs to read
    assert "Accepting to make a red check go green" in result.output


def test_accept_prints_the_rule_even_when_it_refuses(tmp_path):
    root = _project(tmp_path)
    result = _cli(["canon", "accept", "--path", str(root)])
    assert "Accepting to make a red check go green" in result.output


def test_unchanged_entries_keep_their_original_reference(tmp_path):
    """Re-stamping every line on each accept would churn the file and kill blame."""
    root = _project(tmp_path)
    (root / "docs" / "requirements" / "second.md").write_text(
        REQUIREMENTS_TABLE, encoding="utf-8"
    )
    assert _cli(["canon", "accept", "--path", str(root), "--for", "cmd_001"]).exit_code == 0

    (root / "docs" / "requirements" / "second.md").write_text(
        REQUIREMENTS_TABLE_PRETTIFIED, encoding="utf-8"
    )
    assert _cli(["canon", "accept", "--path", str(root), "--for", "cmd_002"]).exit_code == 0

    entries = {e.path: e for e in load_ledger(canon_lock_path(root))["entries"]}
    assert entries["docs/requirements/requirements.md"].accepted_for == "cmd_001"
    assert entries["docs/requirements/second.md"].accepted_for == "cmd_002"


# ═══════════════════════════════════════════════════════════════════════════
# Merge behaviour — the ledger must not become a conflict generator
# ═══════════════════════════════════════════════════════════════════════════


def _git(root: Path, *args: str, check: bool = True):
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
    )


def _merge_two_branch_accepts(tmp_path: Path, doc_a: str, doc_b: str):
    """Accept `doc_a` on one branch and `doc_b` on another, then merge."""
    root = _project(tmp_path)
    req_dir = root / "docs" / "requirements"
    # A realistic multi-document canon, alphabetically ordered in the ledger.
    for name in ("alpha.md", "beta.md", "gamma.md", "delta.md"):
        (req_dir / name).write_text(REQUIREMENTS_TABLE, encoding="utf-8")

    _git(root, "init", "-q")
    assert _cli(["canon", "accept", "--path", str(root), "--for", "cmd_000"]).exit_code == 0
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    def branch_accept(branch: str, doc: str, reference: str):
        _git(root, "checkout", "-q", "-b", branch, "main" if branch != "main" else "HEAD")
        (req_dir / doc).write_text(
            REQUIREMENTS_TABLE_PRETTIFIED.replace("FR-1", f"FR-{branch}"), encoding="utf-8"
        )
        assert _cli(
            ["canon", "accept", "--path", str(root), "--for", reference]
        ).exit_code == 0
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"accept {doc}")

    _git(root, "branch", "-M", "main")
    branch_accept("wa", doc_a, "cmd_a")
    _git(root, "checkout", "-q", "main")
    branch_accept("wb", doc_b, "cmd_b")
    _git(root, "checkout", "-q", "main")
    merge = _git(root, "merge", "--no-edit", "wa", check=False)
    assert merge.returncode == 0, merge.stdout + merge.stderr
    return root, _git(root, "merge", "--no-edit", "wb", check=False)


def test_accepts_of_far_apart_documents_merge_without_conflict(tmp_path):
    """Two branches accepting DIFFERENT documents must not collide in the ledger."""
    root, merge = _merge_two_branch_accepts(tmp_path, "alpha.md", "gamma.md")
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert "CONFLICT" not in (merge.stdout + merge.stderr)
    # and the merged ledger still matches both files on disk
    entries = ledger_digests(load_ledger(canon_lock_path(root)))
    assert entries["docs/requirements/alpha.md"] != entries["docs/requirements/beta.md"]
    assert entries["docs/requirements/gamma.md"] != entries["docs/requirements/beta.md"]
    assert _check(root).passed is True


def test_accepts_of_adjacent_documents_merge_without_conflict(tmp_path):
    """The hard case: their ledger lines are neighbours (alpha.md / beta.md).

    Git conflicts when two sides change overlapping hunks, and adjacent changed
    lines are the worst case for a line-oriented file. This test pins the real
    boundary of the merge-friendliness claim instead of only testing the easy
    case — if it ever starts failing, the CHANGELOG claim has to be narrowed.
    """
    root, merge = _merge_two_branch_accepts(tmp_path, "alpha.md", "beta.md")
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert "CONFLICT" not in (merge.stdout + merge.stderr)


def test_same_document_accepted_on_both_branches_does_conflict(tmp_path):
    """Negative control: a genuine disagreement MUST surface as a conflict."""
    _root, merge = _merge_two_branch_accepts(tmp_path, "alpha.md", "alpha.md")
    assert merge.returncode != 0
    assert "CONFLICT" in (merge.stdout + merge.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# CoDD's own generated output must never be ledgered by discovery
# ═══════════════════════════════════════════════════════════════════════════


def test_generated_coverage_report_is_not_canon(tmp_path):
    """`codd require --audit` writes it into docs/requirements/ by default.

    Ledgering CoDD's own report would put the tripwire red after an ordinary
    audit run — the alarm-fatigue failure the narrow scope exists to avoid.
    """
    root = _project(tmp_path)
    report = root / "docs" / "requirements" / "coverage_audit_report.md"
    report.write_text("# Requirement Coverage Audit Report\n\nGenerated: x\n", encoding="utf-8")

    scoped = [p.name for p in canon_documents(root, _config(root))]
    assert "coverage_audit_report.md" not in scoped
    assert "requirements.md" in scoped

    _accept(root)
    report.write_text("# Requirement Coverage Audit Report\n\nGenerated: y\n", encoding="utf-8")
    assert _check(root).passed is True, "regenerating the report must not go red"


def test_an_explicit_canon_docs_entry_still_wins(tmp_path):
    """The skip is a DISCOVERY default; an explicit declaration is deliberate."""
    root = _project(
        tmp_path,
        {"canon": {"docs": ["docs/requirements/coverage_audit_report.md"]}},
    )
    report = root / "docs" / "requirements" / "coverage_audit_report.md"
    report.write_text("# report\n", encoding="utf-8")

    scoped = [p.name for p in canon_documents(root, _config(root))]
    assert scoped == ["coverage_audit_report.md"]


# ═══════════════════════════════════════════════════════════════════════════
# Honest scope: this is not an authorization gate, and it says so
# ═══════════════════════════════════════════════════════════════════════════


def test_a_deliberate_edit_plus_accept_is_green_by_design(tmp_path):
    """Pins the documented limit so nobody later claims more than it delivers."""
    root = _project(tmp_path)
    _accept(root)
    (root / "docs" / "requirements" / "requirements.md").write_text(
        "---\ncodd:\n  node_id: req:system\n  type: requirement\n---\n\nrewritten\n",
        encoding="utf-8",
    )
    assert _check(root).passed is False

    assert _cli(["canon", "accept", "--path", str(root), "--for", "cmd_x"]).exit_code == 0
    assert _check(root).passed is True


def test_the_limit_is_stated_where_a_reader_will_find_it():
    """In the module docstrings AND in the ledger file's own header."""
    import codd.canon as canon_mod
    import codd.dag.checks.canon_integrity as check_mod

    assert "not an authorization gate" in canon_mod.__doc__.lower()
    assert "unaware change" in canon_mod.__doc__.lower()
    assert "unaware change" in check_mod.__doc__.lower()
    assert any("NOT AN AUTHORIZATION GATE" in line for line in canon_mod.LEDGER_HEADER)
