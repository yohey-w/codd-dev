"""Extractor abstractions and non-source parsers shared by CoDD backends.

``codd.parsing`` used to be a single ~4,000-line module. It is now a package
split along its extraction clusters:

- :mod:`codd.parsing._shared` — info dataclasses, protocol, regex fallback,
  walk/text helpers shared across clusters
- :mod:`codd.parsing.python_ast` — stdlib ``ast`` backend for Python
- :mod:`codd.parsing.treesitter` — Tree-sitter backend (Python/TS/JS)
- :mod:`codd.parsing.schemas` — SQL DDL and Prisma schema extraction
- :mod:`codd.parsing.api_specs` — OpenAPI / GraphQL / protobuf specs
- :mod:`codd.parsing.iac` — IaC and ops-evidence extractors
- :mod:`codd.parsing.tests_builddeps` — test and build-deps evidence
- :mod:`codd.parsing.filesystem_routes` — filesystem-convention routes

Every name that was importable from the old module (including the
underscore-prefixed helpers other modules rely on) is re-exported here, so
``from codd.parsing import X`` and ``codd.parsing.X`` keep working unchanged.

``hcl2`` deliberately lives in THIS namespace: ``codd.parsing.hcl2`` is the
canonical binding (``None`` when python-hcl2 is missing) and the Terraform
extractor reads it through the package at call time, so monkeypatching
``codd.parsing.hcl2`` still toggles the regex fallback as it always did.
"""

from __future__ import annotations

try:
    import hcl2
except ModuleNotFoundError:
    hcl2 = None

from codd.parsing._shared import (
    ApiSpecInfo,
    BuildDepsInfo,
    ConfigInfo,
    LanguageExtractor,
    PrismaSchemaInfo,
    RegexExtractor,
    SqlSchemaInfo,
    TestInfo,
    _IGNORED_DIR_NAMES,
    _dedupe,
    _extract_braced_body,
    _find_named_blocks,
    _iter_project_files,
    _load_structured_document,
    _load_yaml_documents,
    _make_symbol,
    _normalize_environment,
    _normalize_list,
    _normalize_ws,
    _route_from_decorator,
    _split_csv,
)
from codd.parsing.python_ast import (
    PythonAstExtractor,
    _PY_BUILTIN_CALLS,
    _detect_python_code_patterns_stdlib,
    _extract_python_call_graph_stdlib,
    _extract_python_import_specifiers_stdlib,
    _extract_python_imports_stdlib,
    _extract_python_symbols_stdlib,
    _header_until_colon,
    _matching_paren_index,
    _python_callee_name,
    _python_expr_text,
    _python_function_signature,
    _python_source_segment,
    _python_stdlib,
    _record_python_import,
    _resolve_python_relative_key,
)
from codd.parsing.treesitter import (
    TreeSitterExtractor,
    _TREE_SITTER_LANGUAGE_PACKAGES,
    _TS_BUILTIN_NAMES,
    _build_parser,
    _detect_python_code_patterns,
    _detect_typescript_code_patterns,
    _extract_object_shape,
    _extract_python_call_graph,
    _extract_python_decorators,
    _extract_python_imports_ast,
    _extract_python_symbols_ast,
    _extract_string_literal,
    _extract_ts_call_graph,
    _extract_typescript_heritage,
    _extract_typescript_imports_ast,
    _extract_typescript_symbols,
    _field_text,
    _is_async_node,
    _iter_named_nodes,
    _load_tree_sitter_language,
    _node_text,
    _record_js_import,
    _resolve_js_import,
    _strip_type_annotation,
    _strip_wrapping,
    _unwrap_value_node,
)
from codd.parsing.schemas import (
    PrismaSchemaExtractor,
    SqlDdlExtractor,
    _PRISMA_SCALARS,
    _append_foreign_key,
    _extract_prisma_schema,
    _extract_sql_schema,
    _extract_sql_schema_from_tree,
    _regex_create_index,
    _regex_foreign_keys,
    _sql_first_object_name,
)
from codd.parsing.api_specs import (
    GraphQlExtractor,
    OpenApiExtractor,
    ProtobufExtractor,
    _OPENAPI_METHODS,
    _extract_openapi_properties,
    _graphql_default_value,
    _graphql_field_to_dict,
    _graphql_input_value_to_dict,
    _graphql_type_to_string,
    _load_graphql_symbols,
    _parse_graphql_fields,
    _parse_proto_enum_values,
    _parse_proto_fields,
    _parse_proto_rpcs,
)
from codd.parsing.iac import (
    AnsibleExtractor,
    DockerComposeExtractor,
    DockerfileExtractor,
    GitHubActionsExtractor,
    KubernetesExtractor,
    OpsEvidenceExtractor,
    PrometheusRulesExtractor,
    TerraformExtractor,
)
from codd.parsing.tests_builddeps import (
    BuildDepsExtractor,
    TestExtractor,
    tomllib,
)
from codd.parsing.filesystem_routes import (
    FileSystemRouteExtractor,
    FilesystemRouteInfo,
    _apply_dynamic_route_rule,
    _expand_braced_route_pattern,
    _expand_route_patterns,
    _filesystem_route_path,
    _format_filesystem_route_url,
    _is_ignored_route_segment,
    _iter_filesystem_route_files,
    _match_filesystem_route_kind,
    _matching_route_pattern,
    _normalize_dynamic_route_rules,
    _normalize_filesystem_route_segments,
    _normalize_filesystem_url_path,
    _pattern_identifies_route_marker,
    _resolve_route_base_dir,
    _rewrite_dynamic_route_segment,
    _route_file_segment,
    _split_filesystem_route_segment,
)


def get_extractor(language: str, category: str = "source") -> LanguageExtractor:
    """Select the best available extractor for a language/category pair.

    Contract Kernel Cut Condition A: selection is REGISTRY-DATA-driven (see
    :mod:`codd.parsing.extractor_registry`) — the language NAMES live in a data
    table, the core dispatches by a table lookup, NOT a ``if language ==``
    ladder. Falls through to :class:`RegexExtractor` (best-effort analysis) for
    any unknown/unsupported language. Behaviour is byte-identical to the former
    inline ladder.
    """
    from codd.parsing.extractor_registry import select_extractor

    return select_extractor(language, category)


__all__ = [
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
]
