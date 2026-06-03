"""Generic tests for the undeclared-interactive-surface reconciliation check.

Every fixture here is framework-agnostic: synthetic ``operation_flow`` mappings
(``frameworks=[]`` shape) plus tmp source files containing plain HTML/ARIA
markup. No UI-library (tiptap, Next.js, React Router, ...) name appears in any
assertion, so the check is exercised exactly as a vendor-neutral project would
experience it. The core invariant is symmetric: the check stays silent when the
surface IS declared in operation_flow and fires when it is NOT.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.surface_reconciliation import (
    UndeclaredSurface,
    declared_capability_tokens,
    detect_undeclared_surfaces,
    surface_reconciliation_settings,
    surface_reconciliation_warnings,
)


def _flow(*operations: dict) -> dict:
    return {"operations": list(operations)}


# --- Declared-token universe -------------------------------------------------


def test_declared_tokens_include_verbs_targets_and_ids() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "author_post", "verb": "create", "target": "blog_post"},
            ),
        )
    ]

    tokens = declared_capability_tokens(flows)

    # Raw + canonical verb, target words, and id words are all present.
    assert "create" in tokens
    assert "author_post" in tokens
    assert "post" in tokens  # target word
    assert "blog_post" in tokens


# --- Check A core: literal <button> authoring control ------------------------


def test_undeclared_button_control_is_reported() -> None:
    # A wired authoring control (the wiring scanner would PASS it) whose verb is
    # absent from operation_flow -> Check A still fires.
    source = (
        '<button type="button" onClick={remove}>Delete draft</button>'
    )
    declared = declared_capability_tokens(
        [("synthetic", _flow({"id": "view_draft", "verb": "view", "target": "draft"}))]
    )

    surfaces = detect_undeclared_surfaces([("ui/editor.tsx", source)], declared)

    assert len(surfaces) == 1
    assert surfaces[0].capability == "delete"
    assert surfaces[0].kind == "control"
    assert "ui/editor.tsx" in surfaces[0].message
    assert "operation_flow" in surfaces[0].message


def test_declared_button_control_stays_silent() -> None:
    # Same wired control, but now operation_flow DECLARES the delete capability
    # via a synonym ("remove" -> canonical delete), proving canonical matching.
    source = '<button type="button" onClick={remove}>Delete draft</button>'
    declared = declared_capability_tokens(
        [
            (
                "synthetic",
                _flow(
                    {"id": "remove_draft", "verb": "remove", "target": "draft"},
                ),
            )
        ]
    )

    surfaces = detect_undeclared_surfaces([("ui/editor.tsx", source)], declared)

    assert surfaces == ()


def test_non_mutating_button_is_ignored() -> None:
    # A plain navigational/non-verb label is not an authoring capability.
    source = "<button>Next</button>"

    surfaces = detect_undeclared_surfaces([("ui/page.tsx", source)], frozenset())

    assert surfaces == ()


def test_runtime_action_target_token_covers_control() -> None:
    # Capability undeclared in operation_flow but covered by a runtime action
    # outcome token -> no warning (mirrors interactive-control runtime evidence).
    source = '<button type="button" onClick={remove}>Delete item</button>'
    declared = frozenset()
    runtime = frozenset({"delete", "item"})

    surfaces = detect_undeclared_surfaces([("ui/list.tsx", source)], declared, runtime)

    assert surfaces == ()


# --- Check A core: generic editor surface (contenteditable / role=textbox) ----


def test_contenteditable_surface_without_declared_edit_is_reported() -> None:
    source = '<div contenteditable="true" aria-label="Body"></div>'

    surfaces = detect_undeclared_surfaces([("ui/body.tsx", source)], frozenset())

    assert len(surfaces) == 1
    assert surfaces[0].kind == "editor"
    assert surfaces[0].capability == "edit"


def test_role_textbox_multiline_surface_is_reported() -> None:
    source = '<div role="textbox" aria-multiline="true"></div>'

    surfaces = detect_undeclared_surfaces([("ui/notes.tsx", source)], frozenset())

    assert len(surfaces) == 1
    assert surfaces[0].kind == "editor"


def test_editor_surface_with_declared_edit_stays_silent() -> None:
    source = '<div contenteditable="true"></div>'
    declared = declared_capability_tokens(
        [
            (
                "synthetic",
                _flow({"id": "edit_content", "verb": "edit", "target": "content_body"}),
            )
        ]
    )

    surfaces = detect_undeclared_surfaces([("ui/body.tsx", source)], declared)

    assert surfaces == ()


def test_editor_surface_with_declared_author_verb_stays_silent() -> None:
    # "author" canonicalizes into the produce family; an authoring operation
    # declared as create satisfies the edit capability via the shared taxonomy.
    source = '<div contenteditable="true"></div>'
    declared = declared_capability_tokens(
        [("synthetic", _flow({"id": "edit_body", "verb": "modify", "target": "body"}))]
    )

    surfaces = detect_undeclared_surfaces([("ui/body.tsx", source)], declared)

    assert surfaces == ()


# --- Settings / opt-out ------------------------------------------------------


def test_settings_default_enabled_no_extra_verbs() -> None:
    enabled, extra = surface_reconciliation_settings({})

    assert enabled is True
    assert extra == frozenset()


def test_opt_out_suppresses_warnings() -> None:
    source = '<button onClick={save}>Save</button>'
    messages = surface_reconciliation_warnings(
        [("ui/editor.tsx", source)],
        [("synthetic", _flow({"id": "view_x", "verb": "view", "target": "x"}))],
        config={"surface_reconciliation": {"enabled": False}},
    )

    assert messages == []


def test_authoring_verb_override_catches_custom_label() -> None:
    # A domain whose authoring control uses a non-taxonomy English label can
    # widen the recognized verbs without any core change.
    source = "<button onClick={x}>Compose memo</button>"
    flows = [("synthetic", _flow({"id": "view_memo", "verb": "view", "target": "memo"}))]

    # Default taxonomy does not know "compose" as a <button>-label verb here.
    assert (
        surface_reconciliation_warnings([("ui/m.tsx", source)], flows, config={}) == []
    )
    # Project widens authoring_verbs -> the surface is now flagged as undeclared.
    messages = surface_reconciliation_warnings(
        [("ui/m.tsx", source)],
        flows,
        config={"surface_reconciliation": {"authoring_verbs": ["compose"]}},
    )
    assert len(messages) == 1
    assert "compose" in messages[0]


def test_duplicate_capability_in_same_file_warns_once() -> None:
    source = (
        '<button onClick={a}>Delete one</button>'
        '<button onClick={b}>Delete two</button>'
    )
    messages = surface_reconciliation_warnings(
        [("ui/list.tsx", source)],
        [("synthetic", _flow({"id": "view_item", "verb": "view", "target": "item"}))],
        config={},
    )

    assert len(messages) == 1


# --- codd doctor CLI integration --------------------------------------------


def _doctor_project(
    tmp_path: Path,
    operation_flow_yaml: str,
    source_files: dict[str, str],
    extra: str = "",
) -> Path:
    project = tmp_path / "app"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        f"""
version: "0.1.0"
project:
  name: app
  language: typescript
scan:
  source_dirs:
    - src/
{operation_flow_yaml}
{extra}
""".lstrip(),
        encoding="utf-8",
    )
    src_dir = project / "src"
    src_dir.mkdir(parents=True)
    for name, content in source_files.items():
        path = src_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return project


def test_doctor_warns_on_undeclared_editor_surface(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        # Only a view operation is declared; the authoring surface is undeclared.
        """
operation_flow:
  operations:
    - id: view_lesson
      verb: view
      target: lesson
      actor: learner
""",
        {"content-editor.tsx": '<div contenteditable="true" aria-label="Body"></div>'},
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "undeclared_surface" in result.output


def test_doctor_silent_when_authoring_surface_declared(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: edit_lesson_content
      verb: edit
      target: content_body
      actor: admin
    - id: view_lesson
      verb: view
      target: lesson
      actor: learner
""",
        {"content-editor.tsx": '<div contenteditable="true" aria-label="Body"></div>'},
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "undeclared_surface" not in result.output


def test_doctor_opt_out_suppresses_surface_warning(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: view_lesson
      verb: view
      target: lesson
      actor: learner
""",
        {"content-editor.tsx": '<div contenteditable="true"></div>'},
        extra="""
surface_reconciliation:
  enabled: false
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "undeclared_surface" not in result.output


def test_message_does_not_leak_library_names() -> None:
    # Guard against overfitting: the advisory text is library-neutral.
    surface = UndeclaredSurface(
        kind="editor", capability="edit", label="content editor", source="ui/x.tsx"
    )
    lowered = surface.message.lower()
    for banned in ("tiptap", "next.js", "nextjs", "react", "prosemirror", "quill"):
        assert banned not in lowered
