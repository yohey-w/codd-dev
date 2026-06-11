"""RF6: canonical HITL verb lifecycle (derive → show → approve → apply) + hidden aliases.

Every proposal/review/apply flow exposes the same canonical verb set; the
legacy verbs (``operations merge``, ``contract suggest``/``adopt``,
``llm list``) keep working as *hidden* deprecated aliases. For each renamed
command this module proves:

* canonical and alias invocations behave identically (same exit code, same
  stdout, same files written) against identical fixtures;
* the alias emits exactly one stderr deprecation note while the canonical
  verb stays silent;
* the alias never appears in the group's ``--help`` command listing, while
  the canonical verb does;
* group help states the canonical lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml
from click.testing import CliRunner

import codd.operations_derive as opx
from codd.cli import main

_CLICK_VERSION = tuple(int(part) for part in click.__version__.split(".")[:2])

# (group, canonical verb, deprecated alias)
RENAMES = [
    ("operations", "apply", "merge"),
    ("contract", "derive", "suggest"),
    ("contract", "apply", "adopt"),
    ("llm", "show", "list"),
]


def _runner() -> CliRunner:
    """CliRunner that keeps stdout/stderr separate across the supported click range."""
    if _CLICK_VERSION < (8, 2):
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _note(group: str, alias: str, canonical: str) -> str:
    return f"note: 'codd {group} {alias}' is deprecated; use 'codd {group} {canonical}'."


def _listed_commands(help_output: str) -> set[str]:
    """The subcommand names rendered in a group's ``Commands:`` section."""
    names: set[str] = set()
    in_commands = False
    for line in help_output.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if in_commands and line.startswith("  "):
            names.add(line.split()[0])
    return names


# --- fixtures -----------------------------------------------------------------


def _operations_project(root: Path) -> Path:
    """Project with one declared operation and one approved proposal entry."""
    codd_dir = root / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {"operation_flow": {"operations": [
                {"id": "list_records", "actor": "op", "verb": "list", "target": "record"},
            ]}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    artifact = opx.ProposalArtifact(
        generated_at="2026-01-01T00:00:00+00:00",
        proposals=[
            opx.ProposedOperation(
                id="archive_record", actor="op", verb="archive", target="record",
                expected_outcomes=["record archived"], approved=True,
            ),
        ],
    )
    opx.write_proposal_artifact(codd_dir / "operations_proposal.yaml", artifact)
    return codd_dir


def _contract_project(root: Path) -> Path:
    """Minimal project the deterministic contract derive/apply flow can run on."""
    codd_dir = root / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump({"project_name": "demo"}, sort_keys=False),
        encoding="utf-8",
    )
    req_dir = root / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "requirements.md").write_text("# Requirements\n\n- list records\n", encoding="utf-8")
    return codd_dir


def _llm_project(root: Path) -> None:
    """Project with a cached consideration set (one approved, one pending)."""
    cache_dir = root / ".codd" / "consideration_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "generated.json").write_text(
        json.dumps(
            {
                "provider_id": "fake",
                "design_doc_sha": "abc",
                "generated_at": "2026-01-01T00:00:00Z",
                "considerations": [
                    {"id": "one", "description": "one description", "approval_status": "pending"},
                    {"id": "two", "description": "two description", "approval_status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )


# --- help surface: canonical listed, alias hidden, lifecycle stated ------------


def test_canonical_verbs_listed_in_group_help() -> None:
    runner = _runner()
    for group, canonical, _alias in RENAMES:
        result = runner.invoke(main, [group, "--help"])
        assert result.exit_code == 0, result.output
        assert canonical in _listed_commands(result.output), (group, canonical)


def test_aliases_hidden_from_group_help() -> None:
    runner = _runner()
    for group, _canonical, alias in RENAMES:
        result = runner.invoke(main, [group, "--help"])
        assert result.exit_code == 0, result.output
        assert alias not in _listed_commands(result.output), (group, alias)


def test_group_help_states_canonical_lifecycle() -> None:
    runner = _runner()
    expected = {
        "operations": "derive → show → approve → apply",
        "contract": "derive → show → apply",
        "llm": "derive → show → approve",
        "plan": "derive → show → approve",
    }
    for group, lifecycle in expected.items():
        result = runner.invoke(main, [group, "--help"])
        assert result.exit_code == 0, result.output
        assert "Lifecycle" in result.output, group
        assert lifecycle in result.output, group


def test_group_help_does_not_emit_deprecation_note() -> None:
    runner = _runner()
    for group in ("operations", "contract", "llm"):
        result = runner.invoke(main, [group, "--help"])
        assert "deprecated;" not in result.stderr


# --- alias == canonical behavior (same fixture, same outputs/files) ------------


def test_operations_apply_and_merge_are_identical(tmp_path: Path) -> None:
    runner = _runner()
    results = {}
    for verb in ("apply", "merge"):
        root = tmp_path / verb
        root.mkdir()
        codd_dir = _operations_project(root)
        result = runner.invoke(main, ["operations", verb, "--path", str(root)])
        assert result.exit_code == 0, result.output
        results[verb] = (result, (codd_dir / "codd.yaml").read_text(encoding="utf-8"))

    canonical, alias = results["apply"], results["merge"]
    assert canonical[0].stdout == alias[0].stdout
    assert canonical[0].exit_code == alias[0].exit_code
    assert canonical[1] == alias[1]  # codd.yaml written identically
    assert "Merged 1 operation" in canonical[0].stdout
    assert _note("operations", "merge", "apply") in alias[0].stderr
    assert "deprecated" not in canonical[0].stderr


def test_contract_derive_and_suggest_are_identical(tmp_path: Path) -> None:
    runner = _runner()
    results = {}
    for verb in ("derive", "suggest"):
        root = tmp_path / verb
        root.mkdir()
        codd_dir = _contract_project(root)
        result = runner.invoke(main, ["contract", verb, "--path", str(root)])
        assert result.exit_code == 0, result.output
        proposal = (codd_dir / "contract_proposal.yaml").read_text(encoding="utf-8")
        results[verb] = (result, proposal)

    canonical, alias = results["derive"], results["suggest"]
    assert canonical[0].stdout == alias[0].stdout
    assert canonical[0].exit_code == alias[0].exit_code
    assert canonical[1] == alias[1]  # proposal file written identically
    assert _note("contract", "suggest", "derive") in alias[0].stderr
    assert "deprecated" not in canonical[0].stderr


def test_contract_apply_and_adopt_are_identical(tmp_path: Path) -> None:
    runner = _runner()
    results = {}
    for verb in ("apply", "adopt"):
        root = tmp_path / verb
        root.mkdir()
        codd_dir = _contract_project(root)
        derive = runner.invoke(main, ["contract", "derive", "--path", str(root)])
        assert derive.exit_code == 0, derive.output
        result = runner.invoke(main, ["contract", verb, "--path", str(root), "--enable"])
        assert result.exit_code == 0, result.output
        results[verb] = (result, (codd_dir / "codd.yaml").read_text(encoding="utf-8"))

    canonical, alias = results["apply"], results["adopt"]
    assert canonical[0].stdout == alias[0].stdout
    assert canonical[0].exit_code == alias[0].exit_code
    assert canonical[1] == alias[1]  # codd.yaml written identically
    assert "artifact_contract" in canonical[1]
    assert _note("contract", "adopt", "apply") in alias[0].stderr
    assert "deprecated" not in canonical[0].stderr


def test_llm_show_and_list_are_identical(tmp_path: Path) -> None:
    runner = _runner()
    results = {}
    for verb in ("show", "list"):
        root = tmp_path / verb
        root.mkdir()
        _llm_project(root)
        result = runner.invoke(main, ["llm", verb, "--path", str(root)])
        assert result.exit_code == 0, result.output
        results[verb] = result

    canonical, alias = results["show"], results["list"]
    assert canonical.stdout == alias.stdout
    assert canonical.exit_code == alias.exit_code
    assert "one\tpending" in canonical.stdout
    assert "two\tpending" in canonical.stdout
    assert _note("llm", "list", "show") in alias.stderr
    assert "deprecated" not in canonical.stderr


def test_alias_stdout_stays_machine_readable(tmp_path: Path) -> None:
    """The deprecation note goes to stderr only — `--format json` stays parseable."""
    _llm_project(tmp_path)
    result = _runner().invoke(main, ["llm", "list", "--path", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [row["id"] for row in rows] == ["one", "two"]
    assert _note("llm", "list", "show") in result.stderr


def test_alias_help_resolves_to_canonical_command() -> None:
    runner = _runner()
    for group, canonical, alias in RENAMES:
        result = runner.invoke(main, [group, alias, "--help"])
        assert result.exit_code == 0, result.output
        # Usage line names the canonical command, never the alias.
        assert f"{group} {canonical}" in result.output.splitlines()[0]
