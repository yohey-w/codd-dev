"""Extractor abstractions shared by parsing backends."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from codd.extractor import ModuleInfo, Symbol


_TREE_SITTER_LANGUAGE_PACKAGES = {
    "python": "tree_sitter_python",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_typescript",
}


class LanguageExtractor(Protocol):
    """Common interface for language-aware symbol/import extraction."""

    language: str
    category: str

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        """Return symbols found in the given source content."""

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        """Return internal and external imports for the given source content."""

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        """Mutate ModuleInfo with any detected structural patterns."""


class RegexExtractor:
    """Placeholder adapter for the existing regex-based extraction flow."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        return []

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        return {}, set()

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        return None


class TreeSitterExtractor:
    """Skeleton Tree-sitter backend. Parsing logic lands in a follow-up task."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category

    @classmethod
    def is_available(cls, language: str | None = None) -> bool:
        """Return True when the core Tree-sitter package and binding are importable."""
        if find_spec("tree_sitter") is None:
            return False
        if language is None:
            return True
        package_name = _TREE_SITTER_LANGUAGE_PACKAGES.get(language.lower())
        if package_name is None:
            return False
        return find_spec(package_name) is not None

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        return []

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        return {}, set()

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        return None


def get_extractor(language: str, category: str = "source") -> LanguageExtractor:
    """Select the best available extractor for a language/category pair."""
    normalized_language = language.lower()
    normalized_category = category.lower()

    if (
        normalized_category == "source"
        and normalized_language in _TREE_SITTER_LANGUAGE_PACKAGES
        and TreeSitterExtractor.is_available(normalized_language)
    ):
        return TreeSitterExtractor(normalized_language, normalized_category)

    return RegexExtractor(normalized_language, normalized_category)


__all__ = [
    "LanguageExtractor",
    "RegexExtractor",
    "TreeSitterExtractor",
    "get_extractor",
]
