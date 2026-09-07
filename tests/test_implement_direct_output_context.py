"""Direct output declarations are input context as well as output boundaries."""
from pathlib import Path

import pytest

from codd.implementer import DesignContext, ImplementSpec, _build_implementation_prompt


def render(root, outputs, *, expected=(), language="python"):
    return _build_implementation_prompt(
        config={"project": {"language": language, "frameworks": []}},
        design_context=DesignContext("design:fixture", Path("docs/design.md"), "Preserve existing behavior.\n"),
        spec=ImplementSpec("docs/design.md", outputs, expected_outputs=list(expected)),
        dependency_documents=[], conventions=[], coding_principles=None, project_root=root,
    )


@pytest.mark.parametrize("language,path", [("python", "src/cli.py"), ("typescript", "src/library.ts"), ("sql", "data/transform.sql")])
def test_existing_direct_file_is_shown_and_exact_example_is_valid(tmp_path, language, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("EXISTING_CONTRACT_MUST_SURVIVE\n")
    prompt = render(tmp_path, [path], language=language)
    assert "EXISTING_CONTRACT_MUST_SURVIVE" in prompt
    assert f"=== FILE: {path} ===" in prompt
    assert f"{path}/<filename>" not in prompt
    assert "EDIT them, not recreate them from scratch" in prompt


def test_new_exact_file_has_exact_example_without_existing_body(tmp_path):
    prompt = render(tmp_path, ["src/new.py"])
    assert "=== FILE: src/new.py ===" in prompt
    assert "BEGIN EXISTING FILE" not in prompt


def test_directory_is_not_scanned_and_keeps_directory_example(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/private.py").write_text("UNDECLARED_BODY_MUST_NOT_APPEAR\n")
    prompt = render(tmp_path, ["src"])
    assert "=== FILE: src/<filename>.py ===" in prompt
    assert "UNDECLARED_BODY_MUST_NOT_APPEAR" not in prompt


def test_direct_and_expected_same_file_is_included_once(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\n")
    prompt = render(tmp_path, ["source.py"], expected=["source.py"])
    assert prompt.count("BEGIN EXISTING FILE source.py") == 1


def test_direct_output_outward_symlink_is_not_read(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("OUTSIDE_BODY_MUST_NOT_APPEAR\n")
    (tmp_path / "source.py").symlink_to(outside)
    assert "OUTSIDE_BODY_MUST_NOT_APPEAR" not in render(tmp_path, ["source.py"])
