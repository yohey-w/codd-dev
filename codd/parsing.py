"""Extractor abstractions and non-source parsers shared by CoDD backends."""

from __future__ import annotations

import ast
import io
import json
import os
import re
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

try:
    import hcl2
except ModuleNotFoundError:
    hcl2 = None

if TYPE_CHECKING:
    from codd.extractor import ModuleInfo, Symbol


_TREE_SITTER_LANGUAGE_PACKAGES = {
    "sql": "tree_sitter_sql",
    "python": "tree_sitter_python",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_typescript",
}

_JS_IMPORT_SUFFIXES = {
    "javascript": ("", ".js", ".jsx", ".mjs", ".cjs", "/index.js", "/index.jsx"),
    "typescript": ("", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", "/index.ts", "/index.tsx"),
}

_PRISMA_SCALARS = {
    "BigInt",
    "Boolean",
    "Bytes",
    "DateTime",
    "Decimal",
    "Float",
    "Int",
    "Json",
    "String",
    "Unsupported",
}

_IGNORED_DIR_NAMES = {
    ".git",
    ".terraform",
    ".tox",
    ".venv",
    "__pycache__",
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


@dataclass
class ConfigInfo:
    """Normalized representation of infrastructure/configuration files."""

    format: str
    file_path: str
    services: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BuildDepsInfo:
    """Build and dependency metadata extracted from project manifests."""

    file_path: str
    runtime: list[str] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)


@dataclass
class TestInfo:
    """Per-test-file metadata used for test-to-source mapping."""

    file_path: str
    test_functions: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    source_module: str | None = None


@dataclass
class SqlSchemaInfo:
    """Extracted SQL DDL metadata."""

    file_path: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PrismaSchemaInfo:
    """Extracted Prisma schema metadata."""

    file_path: str
    models: list[dict[str, Any]] = field(default_factory=list)
    enums: list[dict[str, Any]] = field(default_factory=list)


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

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        """Return schema information when supported by the extractor."""


class RegexExtractor:
    """Adapter for regex-based extraction and schema parsing."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        from codd import extractor as extractor_module

        return extractor_module._extract_symbols(content, file_path, self.language)

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        from codd import extractor as extractor_module

        return extractor_module._extract_imports(
            content,
            self.language,
            project_root,
            src_dir,
            file_path,
        )

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        from codd import extractor as extractor_module

        extractor_module._detect_code_patterns(mod, content, self.language)
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        if self.category != "schema":
            return None

        normalized_path = Path(file_path).as_posix()
        if self.language == "sql":
            return _extract_sql_schema(content, normalized_path)
        if self.language == "prisma":
            return _extract_prisma_schema(content, normalized_path)
        return None


class TreeSitterExtractor:
    """Enhanced parser for languages where richer structural extraction is available."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category

    @classmethod
    def is_available(cls, language: str | None = None) -> bool:
        """Return True for supported languages, even when using a local fallback parser."""
        if language is None:
            return True
        return language.lower() in _TREE_SITTER_LANGUAGE_PACKAGES

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        if self.language == "python":
            return _extract_python_symbols_ast(content, file_path)
        if self.language in {"typescript", "javascript"}:
            return _extract_typescript_symbols(content, file_path)
        return RegexExtractor(self.language, self.category).extract_symbols(content, file_path)

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        return RegexExtractor(self.language, self.category).extract_imports(
            content,
            file_path,
            project_root,
            src_dir,
        )

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        if self.language == "python":
            _detect_python_code_patterns(mod, content)
            return None
        if self.language in {"typescript", "javascript"}:
            _detect_typescript_code_patterns(mod, content)
            return None
        return RegexExtractor(self.language, self.category).detect_code_patterns(mod, content)

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        return RegexExtractor(self.language, self.category).extract_schema(content, file_path)


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
                    "type": schema.get("type", "") or schema.get("$ref", ""),
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


class DockerComposeExtractor:
    """Extract docker-compose style service definitions."""

    format = "docker-compose"
    file_names = {
        "compose.yaml",
        "compose.yml",
        "docker-compose.override.yaml",
        "docker-compose.override.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }

    def detect_docker_compose(self, project_root: Path) -> list[Path]:
        return [
            file_path
            for file_path in _iter_project_files(project_root, {".yaml", ".yml"})
            if file_path.name in self.file_names
        ]

    def extract_services(self, content: str, file_path: str) -> ConfigInfo:
        payload = _load_structured_document(content, file_path)
        info = ConfigInfo(format=self.format, file_path=file_path)
        if not isinstance(payload, dict):
            return info

        services = payload.get("services") or {}
        if not isinstance(services, dict):
            return info

        for name, config in services.items():
            if not isinstance(config, dict):
                continue
            depends_on = config.get("depends_on", [])
            if isinstance(depends_on, dict):
                depends_on = list(depends_on.keys())
            info.services.append(
                {
                    "name": str(name),
                    "image": str(config.get("image", "")),
                    "ports": _normalize_list(config.get("ports")),
                    "depends_on": _normalize_list(depends_on),
                    "volumes": _normalize_list(config.get("volumes")),
                    "environment": _normalize_environment(config.get("environment")),
                }
            )

        return info


class KubernetesExtractor:
    """Extract Kubernetes resources from YAML manifests."""

    format = "kubernetes"
    supported_kinds = {"ConfigMap", "Deployment", "Ingress", "Service"}

    def detect_k8s_manifests(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, {".yaml", ".yml"}):
            docs = _load_yaml_documents(file_path)
            if any(
                isinstance(doc, dict) and doc.get("kind") in self.supported_kinds
                for doc in docs
            ):
                matches.append(file_path)
        return matches

    def extract_manifests(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            return info

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind not in self.supported_kinds:
                continue

            metadata = doc.get("metadata") or {}
            resource: dict[str, Any] = {
                "kind": kind,
                "name": metadata.get("name", ""),
            }

            if kind == "Deployment":
                spec = doc.get("spec") or {}
                pod_spec = ((spec.get("template") or {}).get("spec") or {})
                resource["replicas"] = spec.get("replicas", 1)
                resource["containers"] = [
                    {
                        "name": container.get("name", ""),
                        "image": container.get("image", ""),
                        "ports": [
                            port.get("containerPort")
                            for port in container.get("ports", []) or []
                            if isinstance(port, dict) and "containerPort" in port
                        ],
                    }
                    for container in pod_spec.get("containers", []) or []
                    if isinstance(container, dict)
                ]
            elif kind == "Service":
                spec = doc.get("spec") or {}
                resource["service_type"] = spec.get("type", "ClusterIP")
                resource["selector"] = spec.get("selector") or {}
                resource["ports"] = [
                    {
                        "port": port.get("port"),
                        "targetPort": port.get("targetPort"),
                    }
                    for port in spec.get("ports", []) or []
                    if isinstance(port, dict)
                ]
            elif kind == "Ingress":
                spec = doc.get("spec") or {}
                resource["rules"] = [
                    {
                        "host": rule.get("host"),
                        "paths": [
                            {
                                "path": path_cfg.get("path"),
                                "service": ((path_cfg.get("backend") or {}).get("service") or {}).get("name"),
                            }
                            for path_cfg in ((rule.get("http") or {}).get("paths") or [])
                            if isinstance(path_cfg, dict)
                        ],
                    }
                    for rule in spec.get("rules", []) or []
                    if isinstance(rule, dict)
                ]
            elif kind == "ConfigMap":
                data = doc.get("data") or {}
                resource["data_keys"] = sorted(str(key) for key in data.keys())

            info.resources.append(resource)

        return info


class TerraformExtractor:
    """Extract Terraform resources when python-hcl2 is available."""

    format = "terraform"

    @classmethod
    def is_available(cls) -> bool:
        return hcl2 is not None or find_spec("hcl2") is not None

    def detect_tf_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".tf"}))

    def extract_resources(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        if hcl2 is None:
            return info

        try:
            parsed = hcl2.load(io.StringIO(content))
        except Exception:
            return info

        for block in parsed.get("resource", []) or []:
            if not isinstance(block, dict):
                continue
            for resource_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    info.resources.append(
                        {
                            "kind": "resource",
                            "type": resource_type,
                            "name": name,
                            "attributes": attributes or {},
                        }
                    )

        for block in parsed.get("data", []) or []:
            if not isinstance(block, dict):
                continue
            for data_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    info.resources.append(
                        {
                            "kind": "data",
                            "type": data_type,
                            "name": name,
                            "attributes": attributes or {},
                        }
                    )

        for block in parsed.get("module", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "module",
                        "name": name,
                        "attributes": attributes or {},
                    }
                )

        for block in parsed.get("variable", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "variable",
                        "name": name,
                        "attributes": attributes or {},
                    }
                )

        return info


class BuildDepsExtractor:
    """Extract build/runtime dependencies from common project manifests."""

    file_names = ("pyproject.toml", "package.json", "go.mod")

    def detect_build_files(self, project_root: Path) -> list[Path]:
        return [project_root / name for name in self.file_names if (project_root / name).exists()]

    def extract_deps(self, content: str, file_type: str, file_path: str = "") -> BuildDepsInfo:
        normalized = file_type.lower()
        if normalized == "pyproject.toml":
            return self._extract_pyproject(content, file_path)
        if normalized == "package.json":
            return self._extract_package_json(content, file_path)
        if normalized == "go.mod":
            return self._extract_go_mod(content, file_path)
        return BuildDepsInfo(file_path=file_path)

    def merge(self, infos: list[BuildDepsInfo]) -> BuildDepsInfo | None:
        if not infos:
            return None
        if len(infos) == 1:
            return infos[0]

        merged = BuildDepsInfo(
            file_path=", ".join(info.file_path for info in infos if info.file_path),
            runtime=[],
            dev=[],
            scripts={},
        )
        for info in infos:
            merged.runtime.extend(info.runtime)
            merged.dev.extend(info.dev)
            merged.scripts.update(info.scripts)
        merged.runtime = _dedupe(merged.runtime)
        merged.dev = _dedupe(merged.dev)
        return merged

    def _extract_pyproject(self, content: str, file_path: str) -> BuildDepsInfo:
        if tomllib is None:
            return BuildDepsInfo(file_path=file_path)

        try:
            payload = tomllib.loads(content)
        except Exception:
            return BuildDepsInfo(file_path=file_path)

        project = payload.get("project") or {}
        runtime = [str(dep) for dep in project.get("dependencies", []) or []]
        dev: list[str] = []
        for deps in (project.get("optional-dependencies") or {}).values():
            if isinstance(deps, list):
                dev.extend(str(dep) for dep in deps)

        scripts = {
            str(name): str(target)
            for name, target in (project.get("scripts") or {}).items()
        }
        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=_dedupe(dev),
            scripts=scripts,
        )

    def _extract_package_json(self, content: str, file_path: str) -> BuildDepsInfo:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return BuildDepsInfo(file_path=file_path)

        return BuildDepsInfo(
            file_path=file_path,
            runtime=sorted((payload.get("dependencies") or {}).keys()),
            dev=sorted((payload.get("devDependencies") or {}).keys()),
            scripts={
                str(name): str(command)
                for name, command in (payload.get("scripts") or {}).items()
            },
        )

    def _extract_go_mod(self, content: str, file_path: str) -> BuildDepsInfo:
        runtime: list[str] = []
        scripts: dict[str, str] = {}
        in_require_block = False

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            if line == "require (":
                in_require_block = True
                continue
            if line == ")" and in_require_block:
                in_require_block = False
                continue
            if line.startswith("require "):
                runtime.append(line.removeprefix("require ").split()[0])
                continue
            if in_require_block:
                runtime.append(line.split()[0])
                continue
            if line.startswith("replace "):
                left, _, right = line.removeprefix("replace ").partition("=>")
                scripts[f"replace:{left.strip()}"] = right.strip()

        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=[],
            scripts=scripts,
        )


class TestExtractor:
    """Extract test metadata from test files."""

    def __init__(self, language: str):
        self.language = language.lower()

    def detect_test_files(self, project_root: Path) -> list[Path]:
        suffixes = {
            "python": {".py"},
            "typescript": {".ts", ".tsx"},
            "javascript": {".js", ".jsx"},
            "go": {".go"},
        }.get(self.language, set())
        if not suffixes:
            return []

        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, suffixes):
            if self._is_test_file(file_path.name):
                matches.append(file_path)
        return matches

    def extract_test_info(self, content: str, file_path: str) -> TestInfo:
        if self.language == "python":
            return self._extract_python(content, file_path)
        if self.language in {"typescript", "javascript"}:
            return self._extract_javascript(content, file_path)
        if self.language == "go":
            return self._extract_go(content, file_path)
        return TestInfo(file_path=file_path)

    def _is_test_file(self, filename: str) -> bool:
        if self.language == "python":
            return filename.startswith("test_") or filename.endswith("_test.py")
        if self.language in {"typescript", "javascript"}:
            return any(
                filename.endswith(suffix)
                for suffix in (
                    ".test.ts",
                    ".spec.ts",
                    ".test.tsx",
                    ".spec.tsx",
                    ".test.js",
                    ".spec.js",
                )
            )
        if self.language == "go":
            return filename.endswith("_test.go")
        return False

    def _extract_python(self, content: str, file_path: str) -> TestInfo:
        tests: list[str] = []
        fixtures: list[str] = []
        pending_fixture = False

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("@pytest.fixture"):
                pending_fixture = True
                continue

            match = re.match(r"^\s*def\s+(\w+)\s*\(", line)
            if not match:
                continue

            name = match.group(1)
            if pending_fixture:
                fixtures.append(name)
                pending_fixture = False
                continue
            if name.startswith("test_"):
                tests.append(name)
            elif name in {"setUp", "tearDown", "setup_method", "teardown_method"}:
                fixtures.append(name)

        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_javascript(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"\b(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]", content)
        fixtures = re.findall(r"\b(beforeEach|afterEach|beforeAll|afterAll)\s*\(", content)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_go(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"^\s*func\s+(Test\w+)\s*\(", content, re.MULTILINE)
        fixtures = re.findall(r"^\s*func\s+(TestMain)\s*\(", content, re.MULTILINE)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)


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


def _load_yaml_documents(file_path: Path) -> list[dict[str, Any]]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        docs = list(yaml.safe_load_all(content))
    except Exception:
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


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


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None or value == "":
        return []
    return [str(value)]


def _normalize_environment(value: Any) -> dict[str, Any] | list[str]:
    if isinstance(value, dict):
        return {str(key): val for key, val in value.items()}
    if isinstance(value, list):
        return [str(item) for item in value]
    return {}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


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


def _extract_sql_schema(content: str, file_path: str) -> SqlSchemaInfo:
    schema = SqlSchemaInfo(file_path=file_path)
    table_pattern = re.compile(r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\);", re.IGNORECASE | re.DOTALL)
    fk_pattern = re.compile(
        r"CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+(\w+)\s*\(([^)]+)\)",
        re.IGNORECASE,
    )
    index_pattern = re.compile(
        r"CREATE\s+INDEX\s+(\w+)\s+ON\s+(\w+)\s*\(([^)]+)\)",
        re.IGNORECASE,
    )
    view_pattern = re.compile(r"CREATE\s+VIEW\s+(\w+)\s+AS", re.IGNORECASE)

    for table_name, body in table_pattern.findall(content):
        columns: list[dict[str, Any]] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue

            fk_match = fk_pattern.search(line)
            if fk_match:
                schema.foreign_keys.append(
                    {
                        "name": fk_match.group(1),
                        "table": table_name,
                        "columns": [column.strip() for column in fk_match.group(2).split(",")],
                        "references_table": fk_match.group(3),
                        "references_columns": [column.strip() for column in fk_match.group(4).split(",")],
                    }
                )
                continue

            if line.upper().startswith(("CONSTRAINT ", "PRIMARY KEY", "UNIQUE ", "FOREIGN KEY")):
                continue

            column_match = re.match(r"^(\w+)\s+([^\s,]+)", line)
            if column_match:
                columns.append(
                    {
                        "name": column_match.group(1),
                        "type": column_match.group(2),
                    }
                )

        schema.tables.append({"name": table_name, "columns": columns})

    for match in index_pattern.finditer(content):
        schema.indexes.append(
            {
                "name": match.group(1),
                "table": match.group(2),
                "columns": [column.strip() for column in match.group(3).split(",")],
            }
        )

    for match in view_pattern.finditer(content):
        schema.views.append({"name": match.group(1)})

    return schema


def _extract_prisma_schema(content: str, file_path: str) -> PrismaSchemaInfo:
    schema = PrismaSchemaInfo(file_path=file_path)
    prisma_scalars = {
        "BigInt",
        "Boolean",
        "Bytes",
        "DateTime",
        "Decimal",
        "Float",
        "Int",
        "Json",
        "String",
    }

    for name, body in _find_named_blocks(content, "model"):
        fields: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for raw_line in body.splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"^(\w+)\s+([^\s]+)(.*)$", line)
            if not match:
                continue

            field_type = match.group(2)
            field = {
                "name": match.group(1),
                "type": field_type,
                "attributes": match.group(3).strip(),
            }
            fields.append(field)

            base_type = field_type.rstrip("?")
            relation_type = base_type.rstrip("[]")
            if base_type.endswith("[]") or (
                relation_type not in prisma_scalars and relation_type[:1].isupper()
            ):
                relations.append(field)

        schema.models.append(
            {
                "name": name,
                "fields": fields,
                "relations": relations,
            }
        )

    for name, body in _find_named_blocks(content, "enum"):
        values = [line.split()[0] for line in body.splitlines() if line.strip()]
        schema.enums.append({"name": name, "values": values})

    return schema


def _extract_python_symbols_ast(content: str, file_path: str) -> list[Symbol]:
    from codd.extractor import Symbol

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return RegexExtractor("python", "source").extract_symbols(content, file_path)

    symbols: list[Symbol] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append(
                Symbol(
                    node.name,
                    "class",
                    file_path,
                    getattr(node, "lineno", 1),
                    decorators=[_normalize_decorator_string(_python_expr_to_string(decorator)) for decorator in node.decorator_list],
                    visibility="private" if node.name.startswith("_") else "public",
                    bases=[_python_expr_to_string(base) for base in node.bases if _python_expr_to_string(base)],
                )
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            symbols.append(
                Symbol(
                    node.name,
                    "function",
                    file_path,
                    getattr(node, "lineno", 1),
                    params=_python_params_to_string(node.args),
                    return_type=_python_expr_to_string(node.returns),
                    decorators=[_normalize_decorator_string(_python_expr_to_string(decorator)) for decorator in node.decorator_list],
                    visibility="private" if node.name.startswith("_") else "public",
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                )
            )

    symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
    return symbols


def _python_expr_to_string(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _python_params_to_string(args: ast.arguments) -> str:
    params: list[str] = []
    positional_args = list(args.posonlyargs) + list(args.args)
    positional_defaults = [None] * (len(positional_args) - len(args.defaults)) + list(args.defaults)

    for argument, default in zip(positional_args, positional_defaults):
        rendered = argument.arg
        if argument.annotation is not None:
            rendered += f": {_python_expr_to_string(argument.annotation)}"
        if default is not None:
            rendered += f" = {_python_expr_to_string(default)}"
        params.append(rendered)

    if args.vararg is not None:
        rendered = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            rendered += f": {_python_expr_to_string(args.vararg.annotation)}"
        params.append(rendered)
    elif args.kwonlyargs:
        params.append("*")

    kw_defaults = list(args.kw_defaults)
    for argument, default in zip(args.kwonlyargs, kw_defaults):
        rendered = argument.arg
        if argument.annotation is not None:
            rendered += f": {_python_expr_to_string(argument.annotation)}"
        if default is not None:
            rendered += f" = {_python_expr_to_string(default)}"
        params.append(rendered)

    if args.kwarg is not None:
        rendered = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            rendered += f": {_python_expr_to_string(args.kwarg.annotation)}"
        params.append(rendered)

    return ", ".join(params)


def _extract_typescript_symbols(content: str, file_path: str) -> list[Symbol]:
    from codd.extractor import Symbol

    symbols: list[Symbol] = []

    def line_number(position: int) -> int:
        return content.count("\n", 0, position) + 1

    for match in re.finditer(
        r"export\s+interface\s+(\w+)(?:\s+extends\s+([^{]+))?\s*\{",
        content,
    ):
        bases = _split_csv(match.group(2))
        symbols.append(
            Symbol(
                match.group(1),
                "interface",
                file_path,
                line_number(match.start()),
                bases=bases,
            )
        )

    for match in re.finditer(r"export\s+type\s+(\w+)(?:<[^>]+>)?\s*=", content):
        symbols.append(
            Symbol(
                match.group(1),
                "type_alias",
                file_path,
                line_number(match.start()),
            )
        )

    for match in re.finditer(r"export\s+enum\s+(\w+)\s*\{", content):
        symbols.append(
            Symbol(
                match.group(1),
                "enum",
                file_path,
                line_number(match.start()),
            )
        )

    for match in re.finditer(
        r"export\s+(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)\s*(?::\s*([^{]+))?\s*\{",
        content,
        re.DOTALL,
    ):
        symbols.append(
            Symbol(
                match.group(1),
                "function",
                file_path,
                line_number(match.start()),
                params=" ".join(match.group(2).split()),
                return_type=" ".join((match.group(3) or "").split()),
                is_async="async" in match.group(0).split("{", 1)[0],
            )
        )

    for match in re.finditer(
        r"export\s+const\s+(\w+)\s*=\s*(async\s+)?\(([^)]*)\)\s*:\s*([^=]+?)\s*=>",
        content,
        re.DOTALL,
    ):
        symbols.append(
            Symbol(
                match.group(1),
                "function",
                file_path,
                line_number(match.start()),
                params=" ".join(match.group(3).split()),
                return_type=" ".join(match.group(4).split()),
                is_async=bool(match.group(2)),
            )
        )

    for match in re.finditer(
        r"export\s+class\s+(\w+)(?:\s+extends\s+([^{\s]+))?(?:\s+implements\s+([^{]+))?\s*\{",
        content,
    ):
        symbols.append(
            Symbol(
                match.group(1),
                "class",
                file_path,
                line_number(match.start()),
                bases=_split_csv(match.group(2)),
                implements=_split_csv(match.group(3)),
            )
        )

    symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
    return symbols


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _detect_python_code_patterns(mod: ModuleInfo, content: str) -> None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return RegexExtractor("python", "source").detect_code_patterns(mod, content)

    routes: list[str] = []
    models: list[str] = []
    background_tasks: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [_python_expr_to_string(base) for base in node.bases]
            if any(base in {"Base", "Model"} or base.endswith(".Model") for base in bases):
                models.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = [_python_expr_to_string(decorator) for decorator in node.decorator_list]
            for decorator in decorators:
                if re.search(r"\.(get|post|put|delete|patch)\(", decorator):
                    routes.append(_normalize_decorator_string(decorator))
                if decorator.endswith(".task") or ".task(" in decorator:
                    background_tasks.append(node.name)

    if routes:
        mod.patterns["api_routes"] = ", ".join(routes)
    if models:
        mod.patterns["db_models"] = ", ".join(models)
    if background_tasks:
        mod.patterns["background_tasks"] = ", ".join(background_tasks)


def _detect_typescript_code_patterns(mod: ModuleInfo, content: str) -> None:
    routes = re.findall(
        r"(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]",
        content,
    )
    models = []
    for match in re.finditer(
        r"export\s+class\s+(\w+)(?:\s+extends\s+([^{\s]+))?(?:\s+implements\s+([^{]+))?\s*\{",
        content,
    ):
        class_name = match.group(1)
        base_name = match.group(2) or ""
        if re.search(r"(Entity|Model)$", base_name):
            models.append(class_name)

    if routes:
        mod.patterns["api_routes"] = ", ".join(routes)
    if models:
        mod.patterns["db_models"] = ", ".join(models)


def _normalize_decorator_string(value: str) -> str:
    if "'" in value and '"' not in value:
        return value.replace("'", '"')
    return value


__all__ = [
    "ApiSpecInfo",
    "BuildDepsExtractor",
    "BuildDepsInfo",
    "ConfigInfo",
    "DockerComposeExtractor",
    "GraphQlExtractor",
    "KubernetesExtractor",
    "LanguageExtractor",
    "OpenApiExtractor",
    "PrismaSchemaInfo",
    "ProtobufExtractor",
    "RegexExtractor",
    "SqlSchemaInfo",
    "TerraformExtractor",
    "TestExtractor",
    "TestInfo",
    "TreeSitterExtractor",
    "get_extractor",
]
