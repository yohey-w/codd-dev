from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


CODE_LAYER_PATHS = [
    *sorted((Path("codd") / "llm").glob("*.py")),
    Path("codd") / "dag" / "extractor.py",
]
HINT_LAYER_PATHS = sorted((Path("codd") / "llm" / "templates").glob("*.yaml"))
COOKBOOK_DIR = Path("docs") / "cookbook" / "llm" / "means_catalog"
DOMAIN_HINTS = {"web_app", "mobile_app", "desktop", "desktop_app", "cli_tool", "backend_api", "embedded"}
CATALOG_VALUES = {
    "cdp_browser",
    "playwright",
    "selenium",
    "cypress",
    "curl_smoke",
    "appium",
    "detox",
    "espresso",
    "xcuitest",
    "winappdriver",
    "autoit",
    "native_ui_test",
    "bats",
    "pytest",
    "pytest_subprocess",
    "shell_integration",
    "click_testing",
    "curl",
    "pact",
    "pact_contract",
    "postman_runner",
    "schemathesis",
    "k6_load",
    "hil",
    "sil",
    "hil_test",
    "sil_test",
    "renode",
}
SPECIFIC_CODE_TERMS = (
    "Web",
    "Mobile",
    "Appium",
    "WinAppDriver",
    "Safari",
    "NextAuth",
    "Cookie",
    "React",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strings_in_test_expression(node: ast.AST) -> set[str]:
    strings: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            strings.add(child.value)
    return strings


def _term_hits(text: str, terms: set[str] | tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for term in terms:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
        if re.search(pattern, text):
            hits.append(term)
    return hits


def test_code_layer_has_no_domain_or_product_terms():
    hits = {
        str(path): _term_hits(_text(path), SPECIFIC_CODE_TERMS)
        for path in CODE_LAYER_PATHS
    }

    assert all(not terms for terms in hits.values()), hits


def test_code_layer_has_no_catalog_domain_or_engine_literals():
    forbidden = DOMAIN_HINTS | CATALOG_VALUES
    hits = {
        str(path): _term_hits(_text(path), forbidden)
        for path in CODE_LAYER_PATHS
    }

    assert all(not terms for terms in hits.values()), hits


def test_hint_layer_contains_catalog_hints():
    catalog_path = Path("codd") / "llm" / "templates" / "verification_means_catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))

    assert {"web_app", "mobile_app", "cli_tool", "backend_api", "embedded"} <= set(catalog)
    assert "cdp_browser" in catalog["web_app"]


def test_code_layer_does_not_branch_on_catalog_values():
    forbidden = DOMAIN_HINTS | CATALOG_VALUES
    hits: dict[str, list[str]] = {}
    for path in CODE_LAYER_PATHS:
        tree = ast.parse(_text(path))
        path_hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                path_hits.extend(sorted(_strings_in_test_expression(node.test) & forbidden))
        if path_hits:
            hits[str(path)] = path_hits

    assert hits == {}


def test_cookbook_samples_are_project_lexicon_snippets():
    expected_files = {
        "web_app.yaml",
        "mobile_app.yaml",
        "desktop.yaml",
        "cli_tool.yaml",
        "backend_api.yaml",
        "embedded.yaml",
    }
    actual_files = {path.name for path in COOKBOOK_DIR.glob("*.yaml")}

    assert actual_files == expected_files
    for path in COOKBOOK_DIR.glob("*.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        catalog = payload.get("verification_means_catalog")
        assert isinstance(catalog, dict)
        assert len(catalog) == 1
        assert all(isinstance(value, list) and value for value in catalog.values())


def test_cookbook_samples_are_not_in_release_include():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    includes = pyproject["tool"]["hatch"]["build"]["include"]

    assert not any(pattern.startswith("docs/cookbook") for pattern in includes)
