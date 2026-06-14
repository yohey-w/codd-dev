import codd.parsing
from codd.discovery import DEFAULT_IGNORED_DIRS
from codd.extractor import ProjectFacts, Symbol
from codd.parsing import PythonAstExtractor, RegexExtractor, TreeSitterExtractor, get_extractor


# ---------------------------------------------------------------------------
# Package import compatibility (parsing.py -> parsing/ split)
# ---------------------------------------------------------------------------
# Names that external modules/tests imported from the old single-module
# ``codd.parsing`` and that MUST keep resolving from the package root.
_PUBLIC_COMPAT_NAMES = [
    # public API (__all__)
    "ApiSpecInfo",
    "BuildDepsExtractor",
    "BuildDepsInfo",
    "ConfigInfo",
    "DockerComposeExtractor",
    "FileSystemRouteExtractor",
    "FilesystemRouteInfo",
    "GraphQlExtractor",
    "KubernetesExtractor",
    "LanguageExtractor",
    "OpenApiExtractor",
    "PythonAstExtractor",
    "PrismaSchemaExtractor",
    "PrismaSchemaInfo",
    "ProtobufExtractor",
    "RegexExtractor",
    "SqlDdlExtractor",
    "SqlSchemaInfo",
    "TerraformExtractor",
    "TestExtractor",
    "TestInfo",
    "TreeSitterExtractor",
    "get_extractor",
    # iac extractors imported by codd.extractor / tests
    "AnsibleExtractor",
    "DockerfileExtractor",
    "GitHubActionsExtractor",
    "OpsEvidenceExtractor",
    "PrometheusRulesExtractor",
]

_PRIVATE_COMPAT_NAMES = [
    # tree-sitter node helpers used by codd.repair_slice
    "_iter_named_nodes",
    "_field_text",
    "_node_text",
    # RF2 alias asserted by tests/test_discovery.py
    "_IGNORED_DIR_NAMES",
    # optional-dependency bindings patched/asserted by tests
    "hcl2",
    "tomllib",
]


def test_package_reexports_public_names():
    for name in _PUBLIC_COMPAT_NAMES:
        assert hasattr(codd.parsing, name), f"codd.parsing.{name} missing after package split"
    assert set(codd.parsing.__all__) <= set(_PUBLIC_COMPAT_NAMES)


def test_package_reexports_private_names_used_by_other_modules():
    for name in _PRIVATE_COMPAT_NAMES:
        assert hasattr(codd.parsing, name), f"codd.parsing.{name} missing after package split"


def test_ignored_dir_names_alias_keeps_identity_with_discovery():
    assert codd.parsing._IGNORED_DIR_NAMES is DEFAULT_IGNORED_DIRS


def test_package_hcl2_patch_controls_terraform_fallback(monkeypatch):
    """Patching codd.parsing.hcl2 (the PACKAGE attribute) must still toggle
    TerraformExtractor's regex fallback, as it did pre-split."""
    tf = 'resource "aws_s3_bucket" "b" {\n  tags = { Team = "x" }\n}\n'
    extractor = codd.parsing.TerraformExtractor()

    assert codd.parsing.hcl2 is not None
    rich = extractor.extract_resources(tf, "main.tf")
    assert rich.resources[0]["attributes"]["tags"] == {"Team": "x"}

    monkeypatch.setattr(codd.parsing, "hcl2", None)
    shallow = extractor.extract_resources(tf, "main.tf")
    # regex fallback keeps the raw text instead of the parsed hcl2 tree
    assert shallow.resources[0]["attributes"]["tags"] == '{ Team = "x" }'


def test_symbol_defaults_are_non_breaking():
    symbol = Symbol("AuthService", "class", "src/auth.py", 10)

    assert symbol.return_type == ""
    assert symbol.decorators == []
    assert symbol.visibility == "public"
    assert symbol.is_async is False


def test_projectfacts_defaults_are_non_breaking():
    facts = ProjectFacts(language="python", source_dirs=["src"])

    assert facts.schemas == {}
    assert facts.api_specs == {}
    assert facts.infra_config == {}
    assert facts.build_deps is None


def test_get_extractor_returns_regex_for_unsupported_language(monkeypatch):
    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: True),
    )

    extractor = get_extractor("java")

    assert isinstance(extractor, RegexExtractor)


def test_get_extractor_returns_python_ast_when_tree_sitter_available(monkeypatch):
    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: True),
    )

    extractor = get_extractor("python")

    assert isinstance(extractor, PythonAstExtractor)


def test_get_extractor_returns_python_ast_when_tree_sitter_missing(monkeypatch):
    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: False),
    )

    extractor = get_extractor("python")

    assert isinstance(extractor, PythonAstExtractor)


def test_test_extractor_recognizes_e2e_ts_suffix():
    # Fact-extraction test→module mapping must see the ``.e2e.ts`` convention,
    # else genuine e2e files are skipped during extraction.
    from codd.parsing import TestExtractor

    ts = TestExtractor("typescript")
    assert ts._is_test_file("tempconv_conversion.e2e.ts") is True
    assert ts._is_test_file("foo.e2e.tsx") is True
    assert ts._is_test_file("foo.test.ts") is True
    assert ts._is_test_file("foo.ts") is False  # non-test source must not match
    js = TestExtractor("javascript")
    assert js._is_test_file("foo.e2e.js") is True
