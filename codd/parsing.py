"""Extractor abstractions and non-source parsers shared by CoDD backends."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

if TYPE_CHECKING:
    from codd.extractor import ModuleInfo, Symbol


_TREE_SITTER_LANGUAGE_PACKAGES = {
    "python": "tree_sitter_python",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_typescript",
}

_IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".tox",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
}

_OPENAPI_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}


@dataclass
class ApiSpecInfo:
    """Normalized representation of a discovered API definition file."""

    format: str
    file_path: str
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)


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


class OpenApiExtractor:
    """Extract endpoints and schema metadata from OpenAPI/Swagger documents."""

    format = "openapi"

    def detect_openapi_files(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, {".json", ".yaml", ".yml"}):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            data = _load_structured_document(content, file_path)
            if isinstance(data, dict) and any(key in data for key in ("openapi", "swagger")):
                matches.append(file_path)
        return matches

    def extract_endpoints(self, content: str, file_path: str) -> ApiSpecInfo:
        data = _load_structured_document(content, file_path)
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        if not isinstance(data, dict):
            return spec

        for path_name, methods in (data.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in _OPENAPI_METHODS or not isinstance(operation, dict):
                    continue
                spec.endpoints.append(
                    {
                        "path": path_name,
                        "method": method.upper(),
                        "operation_id": operation.get("operationId", ""),
                        "summary": operation.get("summary", "") or operation.get("description", ""),
                        "tags": operation.get("tags", []) or [],
                        "parameters": [
                            {
                                "name": parameter.get("name", ""),
                                "in": parameter.get("in", ""),
                                "required": bool(parameter.get("required", False)),
                            }
                            for parameter in operation.get("parameters", []) or []
                            if isinstance(parameter, dict)
                        ],
                        "request_body": sorted(
                            (operation.get("requestBody") or {}).get("content", {}).keys()
                        )
                        if isinstance(operation.get("requestBody"), dict)
                        else [],
                        "responses": sorted((operation.get("responses") or {}).keys()),
                    }
                )

        components = data.get("components") or {}
        for schema_name, schema in (components.get("schemas") or {}).items():
            if not isinstance(schema, dict):
                continue
            spec.schemas.append(
                {
                    "name": schema_name,
                    "kind": "schema",
                    "type": schema.get("type", ""),
                    "required": schema.get("required", []) or [],
                    "properties": _extract_openapi_properties(schema.get("properties") or {}),
                }
            )

        for server in data.get("servers") or []:
            if isinstance(server, dict):
                spec.services.append(
                    {
                        "kind": "server",
                        "url": server.get("url", ""),
                        "description": server.get("description", ""),
                    }
                )

        if not spec.services and data.get("host"):
            base_path = data.get("basePath", "")
            schemes = data.get("schemes") or ["https"]
            spec.services.append(
                {
                    "kind": "server",
                    "url": f"{schemes[0]}://{data['host']}{base_path}",
                    "description": "swagger-host",
                }
            )

        return spec


class GraphQlExtractor:
    """Extract GraphQL types and root operations from SDL files."""

    format = "graphql"

    def detect_graphql_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".gql", ".graphql", ".graphqls"}))

    def extract_schema(self, content: str, file_path: str) -> ApiSpecInfo:
        graphql_objects = _load_graphql_symbols()
        if graphql_objects is not None:
            return self._extract_with_graphql_core(content, file_path, graphql_objects)
        return self._extract_with_regex_fallback(content, file_path)

    def _extract_with_graphql_core(
        self,
        content: str,
        file_path: str,
        graphql_objects: dict[str, Any],
    ) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        parse = graphql_objects["parse"]
        gql_ast = graphql_objects["ast"]

        try:
            document = parse(content)
        except Exception:
            return self._extract_with_regex_fallback(content, file_path)

        for definition in document.definitions:
            if isinstance(definition, gql_ast.ObjectTypeDefinitionNode):
                type_name = definition.name.value
                fields = [_graphql_field_to_dict(field) for field in definition.fields or []]
                if type_name in {"Query", "Mutation", "Subscription"}:
                    for field in fields:
                        spec.endpoints.append(
                            {
                                "name": field["name"],
                                "operation_type": type_name.lower(),
                                "return_type": field["type"],
                                "arguments": field["arguments"],
                            }
                        )
                else:
                    spec.schemas.append(
                        {
                            "name": type_name,
                            "kind": "type",
                            "fields": fields,
                        }
                    )
            elif isinstance(definition, gql_ast.InputObjectTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "input",
                        "fields": [_graphql_input_value_to_dict(field) for field in definition.fields or []],
                    }
                )
            elif isinstance(definition, gql_ast.EnumTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "enum",
                        "values": [value.name.value for value in definition.values or []],
                    }
                )
            elif isinstance(definition, gql_ast.InterfaceTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "interface",
                        "fields": [_graphql_field_to_dict(field) for field in definition.fields or []],
                    }
                )
            elif isinstance(definition, gql_ast.ScalarTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "scalar",
                    }
                )

        return spec

    def _extract_with_regex_fallback(self, content: str, file_path: str) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        for kind in ("type", "input", "interface"):
            for name, block in _find_named_blocks(content, kind):
                fields = _parse_graphql_fields(block)
                if kind == "type" and name in {"Query", "Mutation", "Subscription"}:
                    for field in fields:
                        spec.endpoints.append(
                            {
                                "name": field["name"],
                                "operation_type": name.lower(),
                                "return_type": field["type"],
                                "arguments": field["arguments"],
                            }
                        )
                else:
                    spec.schemas.append(
                        {
                            "name": name,
                            "kind": kind,
                            "fields": fields,
                        }
                    )

        for name, block in _find_named_blocks(content, "enum"):
            values = [line.strip().split()[0] for line in block.splitlines() if line.strip()]
            spec.schemas.append({"name": name, "kind": "enum", "values": values})

        return spec


class ProtobufExtractor:
    """Extract services, RPCs, messages, and enums from protobuf files."""

    format = "protobuf"

    def detect_proto_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".proto"}))

    def extract_services(self, content: str, file_path: str) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)

        for name, block in _find_named_blocks(content, "message"):
            spec.schemas.append(
                {
                    "name": name,
                    "kind": "message",
                    "fields": _parse_proto_fields(block),
                }
            )

        for name, block in _find_named_blocks(content, "enum"):
            spec.schemas.append(
                {
                    "name": name,
                    "kind": "enum",
                    "values": _parse_proto_enum_values(block),
                }
            )

        for service_name, block in _find_named_blocks(content, "service"):
            rpcs = _parse_proto_rpcs(block)
            spec.services.append(
                {
                    "name": service_name,
                    "kind": "service",
                    "rpcs": rpcs,
                }
            )
            for rpc in rpcs:
                spec.endpoints.append(
                    {
                        "name": rpc["name"],
                        "operation_type": "rpc",
                        "request_type": rpc["request_type"],
                        "response_type": rpc["response_type"],
                        "service": service_name,
                    }
                )

        return spec


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


def _extract_openapi_properties(properties: dict[str, Any]) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for prop_name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        item_type = ""
        items = prop.get("items")
        if isinstance(items, dict):
            item_type = items.get("type", "") or items.get("$ref", "")
        extracted.append(
            {
                "name": prop_name,
                "type": prop.get("type", "") or prop.get("$ref", ""),
                "required": bool(prop.get("required", False)),
                "items": item_type,
            }
        )
    return extracted


def _load_structured_document(content: str, file_path: str | Path) -> Any:
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(content)
        return yaml.safe_load(content)
    except Exception:
        return None


def _iter_project_files(project_root: Path, suffixes: set[str]):
    normalized = {suffix.lower() for suffix in suffixes}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
        ]
        for filename in files:
            file_path = Path(root) / filename
            if file_path.suffix.lower() in normalized:
                yield file_path


def _load_graphql_symbols() -> dict[str, Any] | None:
    try:
        from graphql import parse
        from graphql.language import ast as gql_ast
    except ImportError:
        return None
    return {"parse": parse, "ast": gql_ast}


def _graphql_field_to_dict(field: Any) -> dict[str, Any]:
    return {
        "name": field.name.value,
        "type": _graphql_type_to_string(field.type),
        "arguments": [_graphql_input_value_to_dict(argument) for argument in field.arguments or []],
    }


def _graphql_input_value_to_dict(field: Any) -> dict[str, Any]:
    return {
        "name": field.name.value,
        "type": _graphql_type_to_string(field.type),
        "default": _graphql_default_value(field.default_value),
    }


def _graphql_type_to_string(type_node: Any) -> str:
    kind_name = type(type_node).__name__
    if kind_name == "NonNullTypeNode":
        return f"{_graphql_type_to_string(type_node.type)}!"
    if kind_name == "ListTypeNode":
        return f"[{_graphql_type_to_string(type_node.type)}]"
    return type_node.name.value


def _graphql_default_value(value_node: Any) -> Any:
    if value_node is None:
        return None
    if hasattr(value_node, "value"):
        return value_node.value
    return str(value_node)


def _parse_graphql_fields(block: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for raw_line in block.splitlines():
        line = raw_line.split("#", 1)[0].strip().rstrip(",")
        if not line:
            continue
        match = re.match(r"^(\w+)(?:\((.*?)\))?\s*:\s*([!\[\]\w]+)", line)
        if not match:
            continue
        arguments = []
        if match.group(2):
            for arg in match.group(2).split(","):
                arg_match = re.match(r"\s*(\w+)\s*:\s*([!\[\]\w]+)", arg.strip())
                if arg_match:
                    arguments.append(
                        {
                            "name": arg_match.group(1),
                            "type": arg_match.group(2),
                            "default": None,
                        }
                    )
        fields.append(
            {
                "name": match.group(1),
                "type": match.group(3),
                "arguments": arguments,
            }
        )
    return fields


def _find_named_blocks(content: str, keyword: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    pattern = re.compile(rf"\b{keyword}\s+(\w+)\b[^\{{]*\{{", re.MULTILINE)

    for match in pattern.finditer(content):
        brace_index = content.find("{", match.start())
        body = _extract_braced_body(content, brace_index)
        if body is not None:
            matches.append((match.group(1), body))

    return matches


def _extract_braced_body(content: str, brace_index: int) -> str | None:
    if brace_index < 0:
        return None

    depth = 0
    for index in range(brace_index, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[brace_index + 1 : index]
    return None


def _parse_proto_fields(block: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*(optional|required|repeated)?\s*([\w.]+)\s+(\w+)\s*=\s*(\d+)",
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        label = match.group(1) or ""
        fields.append(
            {
                "name": match.group(3),
                "type": match.group(2),
                "field_number": int(match.group(4)),
                "label": label,
            }
        )
    return fields


def _parse_proto_enum_values(block: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\w+)\s*=\s*(-?\d+)", re.MULTILINE)
    for match in pattern.finditer(block):
        values.append({"name": match.group(1), "number": int(match.group(2))})
    return values


def _parse_proto_rpcs(block: str) -> list[dict[str, Any]]:
    rpcs: list[dict[str, Any]] = []
    pattern = re.compile(
        r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?([.\w]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([.\w]+)\s*\)",
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        rpcs.append(
            {
                "name": match.group(1),
                "request_type": match.group(2),
                "response_type": match.group(3),
            }
        )
    return rpcs


__all__ = [
    "ApiSpecInfo",
    "GraphQlExtractor",
    "LanguageExtractor",
    "OpenApiExtractor",
    "ProtobufExtractor",
    "RegexExtractor",
    "TreeSitterExtractor",
    "get_extractor",
]
