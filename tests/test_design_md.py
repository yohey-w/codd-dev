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
