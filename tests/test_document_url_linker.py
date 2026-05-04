"""Tests for document URL extraction from design/requirement text."""

from codd.extractor import DocumentUrlLinkInfo, DocumentUrlLinker


def test_extracts_urls_from_markdown_prose():
    linker = DocumentUrlLinker()

    result = linker.extract_urls("画面遷移は /admin/dashboard → /admin/courses の順に進む。")

    assert result.urls == ["/admin/courses", "/admin/dashboard"]


def test_extracts_urls_from_mermaid_diagram_text():
    linker = DocumentUrlLinker()
    text = """\
graph LR
  A[/admin/dashboard] --> B[/tenant/users]
  B --> C[/my]
"""

    result = linker.extract_urls(text)

    assert result.urls == ["/admin/dashboard", "/my", "/tenant/users"]


def test_extracts_urls_from_code_block_text():
    linker = DocumentUrlLinker()

    result = linker.extract_urls(
        "`GET /api/v1/enrollments`、`POST /api/v1/courses` でデータを取得する。"
    )

    assert result.urls == ["/api/v1/courses", "/api/v1/enrollments"]


def test_normalizes_urls():
    linker = DocumentUrlLinker()

    result = linker.extract_urls("/admin/dashboard/ /admin/dashboard/ /")

    assert result.urls == ["/", "/admin/dashboard"]


def test_uses_custom_url_pattern():
    linker = DocumentUrlLinker({"url_pattern": r"(?:GET|POST) (\/[\w/]+)"})

    result = linker.extract_urls("GET /api/health  POST /api/courses")

    assert result.urls == ["/api/courses", "/api/health"]


def test_empty_text_returns_empty_urls():
    linker = DocumentUrlLinker()

    result = linker.extract_urls("")

    assert result.urls == []


def test_result_includes_node_id():
    linker = DocumentUrlLinker()

    result = linker.extract_urls("/admin/dashboard", node_id="design:admin")

    assert isinstance(result, DocumentUrlLinkInfo)
    assert result.node_id == "design:admin"
