"""H7 — the scaffolded `ai_command` must not burn `--effort max` by default (F-5).

`max` is not a strict upgrade. On distractor-heavy, long-context, tool-driven
work — exactly what CoDD's generate/implement stages are — accuracy can fall as
the reasoning budget grows, and lower "overthinking" correlates with better
agentic outcomes. Baking `max` into every generated project makes that the
standing default for people who will never revisit the line.

A value assertion has no natural negative control (there is no "guard removed"
state to re-run), so instead this file asserts the default from THREE
independent places — template, packaged defaults, and the code constant. If
someone changes one and not the others, the disagreement itself is the finding:
that is what caught the original F-5, where the template and the code had
drifted apart.
"""

from __future__ import annotations

import re

import yaml

from codd.claude_cli import DEFAULT_CLAUDE_EFFORT

FORBIDDEN = "max"


def _effort_of(command: str) -> str | None:
    match = re.search(r"--effort[=\s]+(\S+)", command)
    return match.group(1).strip("\"'") if match else None


def test_scaffolded_ai_command_does_not_default_to_max(make_project):
    project = make_project()
    command = str(project.config().get("ai_command") or "")
    assert command, "no ai_command in the generated config"

    effort = _effort_of(command)
    assert effort is not None, (
        "the generated ai_command pins no --effort at all. Leaving it unset "
        "removes the user's control point, since ai_command is assembled as a "
        f"shell string:\n{command}"
    )
    assert effort != FORBIDDEN, (
        f"`--effort max` is the scaffolded default again (F-5 regression): {command}"
    )


def test_packaged_defaults_agree_with_the_template(repo_root):
    """Second source. Drift between these two is how F-5 survived review."""
    defaults_path = repo_root / "codd" / "defaults.yaml"
    assert defaults_path.exists(), defaults_path
    defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    command = str(defaults.get("ai_command") or "")
    assert command, "defaults.yaml has no ai_command"
    assert _effort_of(command) != FORBIDDEN, f"defaults.yaml still defaults to max: {command}"


def test_code_constant_agrees_with_the_generated_config(make_project):
    """Third source: the constant the CLI injects when --effort is absent."""
    assert DEFAULT_CLAUDE_EFFORT != FORBIDDEN, (
        f"DEFAULT_CLAUDE_EFFORT is {DEFAULT_CLAUDE_EFFORT!r}"
    )
    scaffolded = _effort_of(str(make_project().config().get("ai_command") or ""))
    assert scaffolded == DEFAULT_CLAUDE_EFFORT, (
        "the scaffolded template and the code constant disagree "
        f"({scaffolded!r} vs {DEFAULT_CLAUDE_EFFORT!r}). One of them was "
        "updated and the other was not — this is the exact shape of F-5."
    )


def test_the_template_says_why_so_it_is_not_silently_raised_back(make_project):
    """A bare value gets 'upgraded' by the next reader. The reason must ship."""
    text = make_project().config_text()
    assert "max" in text and "effort" in text.lower(), text
    assert re.search(r"(?i)(not.{0,20}max|NOT `max`|deliberately)", text), (
        "the config sets a non-max default but never explains it; the next "
        "person to read the line will raise it back:\n" + text
    )
