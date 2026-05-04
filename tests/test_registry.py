"""Tests for extractor registry dynamic loading."""

import pytest

from codd.parsing import FileSystemRouteExtractor
from codd.registry import RegistryError, get_extractor, list_extractors, load_extractor


def test_load_extractor_loads_filesystem_route_extractor():
    extractor = load_extractor({"type": "codd.parsing.FileSystemRouteExtractor"})

    assert isinstance(extractor, FileSystemRouteExtractor)


def test_get_extractor_loads_filesystem_route_extractor_from_registry():
    registry = {
        "filesystem_routes": {
            "type": "codd.parsing.FileSystemRouteExtractor",
            "description": "Extract route paths from filesystem routing conventions.",
        }
    }

    extractor = get_extractor("filesystem_routes", registry)

    assert isinstance(extractor, FileSystemRouteExtractor)


def test_load_extractor_raises_registry_error_for_invalid_type_path():
    with pytest.raises(RegistryError, match="Invalid type path"):
        load_extractor({"type": "FileSystemRouteExtractor"})


def test_get_extractor_returns_none_for_unknown_name():
    registry = {"filesystem_routes": {"type": "codd.parsing.FileSystemRouteExtractor"}}

    assert get_extractor("missing", registry) is None


def test_list_extractors_returns_registered_names_in_order():
    registry = {
        "filesystem_routes": {"type": "codd.parsing.FileSystemRouteExtractor"},
        "openapi": {"type": "codd.parsing.OpenApiExtractor"},
    }

    assert list_extractors(registry) == ["filesystem_routes", "openapi"]
