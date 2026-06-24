from __future__ import annotations

from pathlib import Path

from codd.design_md import DesignMdExtractor, REFERENCE_RE


def _write_design_md(
    tmp_path: Path,
    front_matter: str,
    body: str = "# Design Rationale\n\nUse clear UI.",
) -> Path:
    path = tmp_path / "DESIGN.md"
    path.write_text(f"---\n{front_matter.rstrip()}\n---\n{body}", encoding="utf-8")
    return path


def _token_by_id(result, token_id: str):
    return next(token for token in result.tokens if token.id == token_id)


def test_parse_basic(tmp_path):
    path = _write_design_md(
        tmp_path,
        """
version: "1.0"
name: "Project Name"
colors:
  Primary: "#1A73E8"
""",
    )

    result = DesignMdExtractor().extract(path)

    assert result.error == ""
    assert result.metadata == {"version": "1.0", "name": "Project Name"}
    assert result.body_md.startswith("# Design Rationale")
    assert _token_by_id(result, "colors.Primary").value == "#1A73E8"


def test_token_categories(tmp_path):
    path = _write_design_md(
        tmp_path,
        """
colors:
  Primary: "#1A73E8"
typography:
  body: { fontWeight: 400, fontSize: 16 }
rounded:
  md: "8px"
spacing:
  sm: "8px"
components:
  Button.primary:
    background: "{colors.Primary}"
otherTokens:
  badge: "new"
""",
    )

    result = DesignMdExtractor().extract(path)

    categories = {token.id: token.category for token in result.tokens}
    assert categories["colors.Primary"] == "color"
    assert categories["typography.body"] == "typography"
    assert categories["rounded.md"] == "spacing"
    assert categories["spacing.sm"] == "spacing"
    assert categories["components.Button.primary"] == "component"
    assert categories["otherTokens.badge"] == "other"


def test_reference_syntax():
    assert REFERENCE_RE.findall("{colors.Primary}") == ["colors.Primary"]


def test_component_references(tmp_path):
    path = _write_design_md(
        tmp_path,
        """
colors:
  Primary: "#1A73E8"
rounded:
  md: "8px"
components:
  Button.primary:
    background: "{colors.Primary}"
    rounded: "{rounded.md}"
""",
    )

    result = DesignMdExtractor().extract(path)

    token = _token_by_id(result, "components.Button.primary")
    assert token.value == {"background": "{colors.Primary}", "rounded": "{rounded.md}"}
    assert token.references == ["colors.Primary", "rounded.md"]


def test_nested_component_references(tmp_path):
    path = _write_design_md(
        tmp_path,
        """
colors:
  Primary: "#1A73E8"
components:
  Button:
    primary:
      background: "{colors.Primary}"
""",
    )

    result = DesignMdExtractor().extract(path)

    token = _token_by_id(result, "components.Button.primary")
    assert token.category == "component"
    assert token.references == ["colors.Primary"]


def test_malformed_yaml(tmp_path):
    path = _write_design_md(
        tmp_path,
        """
colors:
  Primary: ["#1A73E8"
""",
    )

    result = DesignMdExtractor().extract(path)

    assert result.tokens == []
    assert result.body_md == ""
    assert result.metadata == {}
    assert "parse error:" in result.error


def test_no_frontmatter(tmp_path):
    path = tmp_path / "DESIGN.md"
    content = "# Design Rationale\n\nNo YAML here."
    path.write_text(content, encoding="utf-8")

    result = DesignMdExtractor().extract(path)

    assert result.tokens == []
    assert result.body_md == content
    assert result.metadata == {}
    assert result.error == ""


def test_missing_file(tmp_path):
    result = DesignMdExtractor().extract(tmp_path / "missing" / "DESIGN.md")

    assert result.tokens == []
    assert result.body_md == ""
    assert result.metadata == {}
    assert "not found" in result.error


# ── sink-level path jail (project_root) ────────────────────────
#
# DESIGN.md tokens drive drift/coherence evidence. When ``project_root`` is
# supplied the extractor must resolve+confine the path so an out-of-root
# DESIGN.md is never parsed. Fail-closed is expressed through the existing
# ``error`` field (distinguishable from ``error=""`` success / "not found").


def _outside_design(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "DESIGN.md"
    doc.write_text('---\ncolors:\n  Primary: "#1A73E8"\n---\n# secret\n', encoding="utf-8")
    return doc


def test_extract_parent_traversal_with_root_errors(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_design(tmp_path)
    result = DesignMdExtractor().extract("../outside/DESIGN.md", project_root=project_root)
    assert result.tokens == []
    assert result.metadata == {}
    assert result.error != ""


def test_extract_absolute_outside_with_root_errors(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design(tmp_path)
    result = DesignMdExtractor().extract(str(doc), project_root=project_root)
    assert result.tokens == []
    assert result.error != ""


def test_extract_in_root_symlink_escape_with_root_errors(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design(tmp_path)
    (project_root / "alias.md").symlink_to(doc)
    result = DesignMdExtractor().extract("alias.md", project_root=project_root)
    assert result.tokens == []
    assert result.error != ""


def test_extract_in_root_with_root_still_parses(tmp_path):
    """Anti-false-red: an in-root DESIGN.md with project_root parses normally."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "DESIGN.md").write_text(
        '---\ncolors:\n  Primary: "#1A73E8"\n---\n# Design\n', encoding="utf-8"
    )
    result = DesignMdExtractor().extract("DESIGN.md", project_root=project_root)
    assert result.error == ""
    assert _token_by_id(result, "colors.Primary").value == "#1A73E8"


def test_extract_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior."""
    doc = _outside_design(tmp_path)
    result = DesignMdExtractor().extract(doc)
    assert result.error == ""
    assert _token_by_id(result, "colors.Primary").value == "#1A73E8"
