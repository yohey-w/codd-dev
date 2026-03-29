from codd.extractor import ProjectFacts, Symbol
from codd.parsing import RegexExtractor, TreeSitterExtractor, get_extractor


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


def test_get_extractor_returns_tree_sitter_when_available(monkeypatch):
    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: True),
    )

    extractor = get_extractor("python")

    assert isinstance(extractor, TreeSitterExtractor)


def test_get_extractor_returns_regex_when_tree_sitter_missing(monkeypatch):
    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: False),
    )

    extractor = get_extractor("python")

    assert isinstance(extractor, RegexExtractor)
