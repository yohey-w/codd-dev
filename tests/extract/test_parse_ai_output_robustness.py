"""Robustness of ``_parse_ai_output`` against fenced / multi-doc AI output.

Bug #4 (Flask brownfield dogfood): the model wrapped its whole response in a
```` ```markdown ```` fence and separated documents with ``^---`` frontmatter
blocks instead of ``--- FILE:`` markers. The old parser split ONLY on exact
``--- FILE: <name> ---`` lines and never stripped fences, so 6 of 7 documents
were silently dropped and the one written file began with a stray
```` ```markdown ```` line that broke frontmatter normalization (which needs
``---`` at byte 0).

All inputs here are SYNTHETIC crafted strings — no ``claude --print`` call.
"""

from __future__ import annotations

from pathlib import Path

from codd.extract_ai import _parse_ai_output


def _out(tmp_path: Path) -> Path:
    out = tmp_path / ".codd" / "extract"
    out.mkdir(parents=True)
    return out


# --- (a) leading/trailing fence stripping -----------------------------------


def test_outer_markdown_fence_is_stripped_before_parsing(tmp_path):
    """A whole-response ```` ```markdown ```` wrapper must not leak into the
    first doc's frontmatter (``---`` must land at byte 0)."""
    out = _out(tmp_path)
    raw = (
        "```markdown\n"
        "--- FILE: L1_data_models.md ---\n"
        "---\n"
        "id: L1_data_models\n"
        "---\n"
        "# L1: Data Models\n"
        "```\n"
    )

    files = _parse_ai_output(raw, out)

    assert [p.name for p in files] == ["L1_data_models.md"]
    text = (out / "L1_data_models.md").read_text(encoding="utf-8")
    assert not text.lstrip("\n").startswith("```"), "leading fence leaked into doc"
    assert text.startswith("---\nid: L1_data_models"), text[:40]


# --- (b) fallback split when 0-1 FILE markers but multiple docs --------------


def test_fallback_split_on_frontmatter_docs_when_no_file_markers(tmp_path):
    """0 ``--- FILE:`` markers + multiple ``^---``-delimited frontmatter docs
    => fall back to splitting on the frontmatter blocks (do NOT collapse into
    one giant doc)."""
    out = _out(tmp_path)
    raw = (
        "```markdown\n"
        "---\n"
        "id: L1_data_models\n"
        "layer: L1\n"
        "---\n"
        "# L1: Data Models\n"
        "body one\n"
        "---\n"
        "id: L2_api_endpoints\n"
        "layer: L2\n"
        "---\n"
        "# L2: API Endpoints\n"
        "body two\n"
        "```\n"
    )

    files = _parse_ai_output(raw, out)

    assert len(files) == 2, [p.name for p in files]
    # Each persisted doc starts with its own frontmatter at byte 0.
    for path in files:
        assert path.read_text(encoding="utf-8").startswith("---\n")
    joined = "\n".join(p.read_text(encoding="utf-8") for p in files)
    assert "id: L1_data_models" in joined
    assert "id: L2_api_endpoints" in joined


def test_flask_shaped_mixed_separators_recovers_all_docs(tmp_path):
    """The real Flask shape: an outer fence, several ``^---`` frontmatter docs,
    a single ``--- FILE:`` marker, and per-doc fences. All docs must be
    recovered; none silently dropped; no fence leaks into frontmatter."""
    out = _out(tmp_path)
    raw = (
        "```markdown\n"
        "---\n"
        "id: L5_infra_config\n"
        "layer: L5\n"
        "---\n"
        "# L5: Infra / Config\n"
        "infra body\n"
        "```\n"
        "--- FILE: L6_tests.md ---\n"
        "```markdown\n"
        "---\n"
        "id: L6_tests\n"
        "layer: L6\n"
        "---\n"
        "# L6: Tests\n"
        "tests body\n"
        "```\n"
    )

    files = _parse_ai_output(raw, out)

    ids = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not text.lstrip("\n").startswith("```"), f"fence leak in {path.name}"
        assert text.startswith("---\n"), f"no byte-0 frontmatter in {path.name}: {text[:30]!r}"
        ids.append(text)
    joined = "\n".join(ids)
    # Pre-fix: only L6 survived (1 FILE marker), L5 was dropped.
    assert "id: L5_infra_config" in joined, "L5 doc was dropped (the Flask bug)"
    assert "id: L6_tests" in joined
    assert len(files) >= 2


def test_fallback_split_on_layer_headers_without_frontmatter(tmp_path):
    """No FILE markers and no per-doc frontmatter, but multiple ``# L<n>:``
    headers => split on the layer headers rather than collapsing."""
    out = _out(tmp_path)
    raw = (
        "# L1: Data Models\n"
        "alpha\n"
        "# L2: API Endpoints\n"
        "beta\n"
        "# L3: UI Pages\n"
        "gamma\n"
    )

    files = _parse_ai_output(raw, out)

    assert len(files) == 3, [p.name for p in files]
    joined = "\n".join(p.read_text(encoding="utf-8") for p in files)
    assert "alpha" in joined and "beta" in joined and "gamma" in joined


# --- (c) loud warning when fewer files persisted than expected --------------


def test_warns_when_persisted_count_below_expected(tmp_path):
    """When ``expected_doc_count`` exceeds the number of persisted files, a loud
    warning is surfaced (not a silent drop)."""
    out = _out(tmp_path)
    raw = "--- FILE: only_one.md ---\n# just one\n"
    warnings: list[str] = []

    files = _parse_ai_output(raw, out, expected_doc_count=7, warnings_out=warnings)

    assert len(files) == 1
    assert warnings, "expected a loud under-count warning"
    assert any("7" in w and "1" in w for w in warnings)


def test_no_warning_when_counts_match(tmp_path):
    out = _out(tmp_path)
    raw = "--- FILE: a.md ---\n# a\n--- FILE: b.md ---\n# b\n"
    warnings: list[str] = []

    files = _parse_ai_output(raw, out, expected_doc_count=2, warnings_out=warnings)

    assert len(files) == 2
    assert warnings == []


# --- regression: existing single-document / marker behavior unchanged --------


def test_plain_file_markers_unchanged(tmp_path):
    """No fence, ≥2 ``--- FILE:`` markers => historical marker-split path."""
    out = _out(tmp_path)
    raw = "--- FILE: L1.md ---\n# L1\n--- FILE: nested/L2.md ---\n# L2\n"

    files = _parse_ai_output(raw, out)

    assert sorted(p.name for p in files) == ["L1.md", "L2.md"]
    assert (out / "nested" / "L2.md").read_text(encoding="utf-8").strip() == "# L2"


def test_single_plain_document_without_markers_or_fence(tmp_path):
    """A single plain document (no markers, no fence, one frontmatter) is written
    as one file — fallback must not over-split a legitimately single doc."""
    out = _out(tmp_path)
    raw = "---\nid: only\n---\n# Only Doc\nbody\n"

    files = _parse_ai_output(raw, out)

    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").startswith("---\nid: only")


def test_body_with_legitimate_code_fence_is_preserved(tmp_path):
    """A real code block INSIDE a document body must survive (only the OUTER
    wrapper fence is stripped, not inner content fences)."""
    out = _out(tmp_path)
    raw = (
        "--- FILE: doc.md ---\n"
        "---\n"
        "id: doc\n"
        "---\n"
        "# Doc\n"
        "Example:\n"
        "```python\n"
        "print('hi')\n"
        "```\n"
        "done\n"
    )

    files = _parse_ai_output(raw, out)

    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "```python" in text and "print('hi')" in text


def test_traversal_still_rejected_after_fence_strip(tmp_path):
    """Fail-closed path safety must survive the fence/fallback rework."""
    import pytest

    out = _out(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("ORIGINAL\n", encoding="utf-8")
    raw = "```markdown\n--- FILE: ../../src/app.py ---\nHACKED\n```\n"

    with pytest.raises(ValueError):
        _parse_ai_output(raw, out)
    assert (src / "app.py").read_text(encoding="utf-8") == "ORIGINAL\n"
