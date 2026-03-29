---
codd:
  node_id: design:extract:parsing
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/parsing.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# parsing

> 1 files, 2,334 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `ApiSpecInfo` | `codd/parsing.py:86` | — |
| class | `ConfigInfo` | `codd/parsing.py:97` | — |
| class | `BuildDepsInfo` | `codd/parsing.py:107` | — |
| class | `TestInfo` | `codd/parsing.py:117` | — |
| class | `SqlSchemaInfo` | `codd/parsing.py:127` | — |
| class | `PrismaSchemaInfo` | `codd/parsing.py:138` | — |
| class | `LanguageExtractor` | `codd/parsing.py:146` | bases: Protocol |
| class | `RegexExtractor` | `codd/parsing.py:174` | — |
| class | `TreeSitterExtractor` | `codd/parsing.py:224` | — |
| class | `SqlDdlExtractor` | `codd/parsing.py:314` | — |
| class | `PrismaSchemaExtractor` | `codd/parsing.py:356` | — |
| class | `OpenApiExtractor` | `codd/parsing.py:1340` | — |
| class | `GraphQlExtractor` | `codd/parsing.py:1432` | — |
| class | `ProtobufExtractor` | `codd/parsing.py:1548` | — |
| class | `DockerComposeExtractor` | `codd/parsing.py:1600` | — |
| class | `KubernetesExtractor` | `codd/parsing.py:1650` | — |
| class | `TerraformExtractor` | `codd/parsing.py:1742` | — |
| class | `BuildDepsExtractor` | `codd/parsing.py:1856` | — |
| class | `TestExtractor` | `codd/parsing.py:1973` | — |
| function | `extract_symbols` | `codd/parsing.py:152` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:155` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:164` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:167` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:170` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `extract_symbols` | `codd/parsing.py:181` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:186` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:203` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:209` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:220` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `is_available` | `codd/parsing.py:234` | `is_available(cls, language: str | None = None) -> bool` |
| function | `extract_symbols` | `codd/parsing.py:245` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:258` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:277` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:294` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:297` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `is_available` | `codd/parsing.py:325` | `is_available(cls) -> bool` |
| function | `extract_symbols` | `codd/parsing.py:328` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:331` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:340` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:343` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:352` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `extract_symbols` | `codd/parsing.py:362` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:365` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:374` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:377` | `extract_schema(self, content: str, file_path: str | Path) -> PrismaSchemaInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:380` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `visit` | `codd/parsing.py:611` | `visit(node: Any, decorators: list[str] | None = None)` |
| function | `visit` | `codd/parsing.py:732` | `visit(node: Any)` |
| function | `visit` | `codd/parsing.py:877` | `visit(node: Any)` |
| function | `detect_openapi_files` | `codd/parsing.py:1345` | `detect_openapi_files(self, project_root: Path) -> list[Path]` |
| function | `extract_endpoints` | `codd/parsing.py:1357` | `extract_endpoints(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_graphql_files` | `codd/parsing.py:1437` | `detect_graphql_files(self, project_root: Path) -> list[Path]` |
| function | `extract_schema` | `codd/parsing.py:1440` | `extract_schema(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_proto_files` | `codd/parsing.py:1553` | `detect_proto_files(self, project_root: Path) -> list[Path]` |
| function | `extract_services` | `codd/parsing.py:1556` | `extract_services(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_docker_compose` | `codd/parsing.py:1613` | `detect_docker_compose(self, project_root: Path) -> list[Path]` |
| function | `extract_services` | `codd/parsing.py:1620` | `extract_services(self, content: str, file_path: str) -> ConfigInfo` |
| function | `detect_k8s_manifests` | `codd/parsing.py:1656` | `detect_k8s_manifests(self, project_root: Path) -> list[Path]` |
| function | `extract_manifests` | `codd/parsing.py:1667` | `extract_manifests(self, content: str, file_path: str) -> ConfigInfo` |
| function | `is_available` | `codd/parsing.py:1756` | `is_available(cls) -> bool` |
| function | `detect_tf_files` | `codd/parsing.py:1759` | `detect_tf_files(self, project_root: Path) -> list[Path]` |
| function | `extract_resources` | `codd/parsing.py:1762` | `extract_resources(self, content: str, file_path: str) -> ConfigInfo` |
| function | `detect_build_files` | `codd/parsing.py:1861` | `detect_build_files(self, project_root: Path) -> list[Path]` |
| function | `extract_deps` | `codd/parsing.py:1864` | `extract_deps(self, content: str, file_type: str, file_path: str = "") -> BuildDepsInfo` |
| function | `merge` | `codd/parsing.py:1874` | `merge(self, infos: list[BuildDepsInfo]) -> BuildDepsInfo | None` |
| function | `extract_call_graph` | `codd/parsing.py:1969` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `detect_test_files` | `codd/parsing.py:1979` | `detect_test_files(self, project_root: Path) -> list[Path]` |
| function | `extract_test_info` | `codd/parsing.py:1995` | `extract_test_info(self, content: str, file_path: str) -> TestInfo` |
| function | `extract_call_graph` | `codd/parsing.py:2060` | `extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]` |
| function | `get_extractor` | `codd/parsing.py:2064` | `get_extractor(language: str, category: str = "source") -> LanguageExtractor` |






## Public API

- `ApiSpecInfo`
- `ConfigInfo`
- `BuildDepsInfo`
- `TestInfo`
- `SqlSchemaInfo`
- `PrismaSchemaInfo`
- `LanguageExtractor`
- `extract_symbols`
- `extract_imports`
- `detect_code_patterns`
- `extract_schema`
- `extract_call_graph`
- `RegexExtractor`
- `extract_symbols`
- `extract_imports`
- `detect_code_patterns`
- `extract_schema`
- `extract_call_graph`
- `TreeSitterExtractor`
- `is_available`
- `extract_symbols`
- `extract_imports`
- `detect_code_patterns`
- `extract_schema`
- `extract_call_graph`
- `SqlDdlExtractor`
- `is_available`
- `extract_symbols`
- `extract_imports`
- `detect_code_patterns`
- `extract_schema`
- `extract_call_graph`
- `PrismaSchemaExtractor`
- `extract_symbols`
- `extract_imports`
- `detect_code_patterns`
- `extract_schema`
- `extract_call_graph`
- `visit`
- `visit`
- `visit`
- `OpenApiExtractor`
- `detect_openapi_files`
- `extract_endpoints`
- `GraphQlExtractor`
- `detect_graphql_files`
- `extract_schema`
- `ProtobufExtractor`
- `detect_proto_files`
- `extract_services`
- `DockerComposeExtractor`
- `detect_docker_compose`
- `extract_services`
- `KubernetesExtractor`
- `detect_k8s_manifests`
- `extract_manifests`
- `TerraformExtractor`
- `is_available`
- `detect_tf_files`
- `extract_resources`
- `BuildDepsExtractor`
- `detect_build_files`
- `extract_deps`
- `merge`
- `extract_call_graph`
- `TestExtractor`
- `detect_test_files`
- `extract_test_info`
- `extract_call_graph`
- `get_extractor`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `TreeSitterExtractor.__init__` | `RegexExtractor` | `codd/parsing.py:230` | no |
| `TreeSitterExtractor.extract_symbols` | `extract_symbols` | `codd/parsing.py:255` | no |
| `TreeSitterExtractor.extract_symbols` | `extract_symbols` | `codd/parsing.py:256` | no |
| `TreeSitterExtractor.extract_imports` | `extract_imports` | `codd/parsing.py:274` | no |
| `TreeSitterExtractor.extract_imports` | `extract_imports` | `codd/parsing.py:275` | no |
| `TreeSitterExtractor.detect_code_patterns` | `detect_code_patterns` | `codd/parsing.py:289` | no |
| `TreeSitterExtractor.detect_code_patterns` | `detect_code_patterns` | `codd/parsing.py:291` | no |
| `TreeSitterExtractor.extract_schema` | `extract_schema` | `codd/parsing.py:295` | no |
| `SqlDdlExtractor.__init__` | `RegexExtractor` | `codd/parsing.py:321` | no |
| `SqlDdlExtractor.is_available` | `is_available` | `codd/parsing.py:326` | no |
| `SqlDdlExtractor.extract_schema` | `extract_schema` | `codd/parsing.py:349` | no |
| `_extract_python_symbols_ast.visit` | `visit` | `codd/parsing.py:615` | no |
| `_extract_python_symbols_ast.visit` | `visit` | `codd/parsing.py:634` | no |
| `_extract_python_symbols_ast.visit` | `visit` | `codd/parsing.py:655` | no |
| `_extract_python_symbols_ast.visit` | `visit` | `codd/parsing.py:659` | no |
| `_extract_python_symbols_ast` | `visit` | `codd/parsing.py:661` | no |
| `_extract_typescript_symbols.visit` | `visit` | `codd/parsing.py:736` | no |
| `_extract_typescript_symbols.visit` | `visit` | `codd/parsing.py:822` | no |
| `_extract_typescript_symbols` | `visit` | `codd/parsing.py:824` | no |
| `_detect_python_code_patterns.visit` | `visit` | `codd/parsing.py:888` | no |
| `_detect_python_code_patterns.visit` | `visit` | `codd/parsing.py:898` | no |
| `_detect_python_code_patterns.visit` | `visit` | `codd/parsing.py:902` | no |
| `_detect_python_code_patterns` | `visit` | `codd/parsing.py:904` | no |
| `_extract_sql_schema_from_tree` | `SqlSchemaInfo` | `codd/parsing.py:1173` | no |
| `_extract_sql_schema` | `SqlSchemaInfo` | `codd/parsing.py:1223` | no |
| `_extract_sql_schema` | `is_available` | `codd/parsing.py:1224` | no |
| `_extract_prisma_schema` | `PrismaSchemaInfo` | `codd/parsing.py:1282` | no |
| `OpenApiExtractor.extract_endpoints` | `ApiSpecInfo` | `codd/parsing.py:1359` | no |
| `GraphQlExtractor._extract_with_graphql_core` | `ApiSpecInfo` | `codd/parsing.py:1452` | no |
| `GraphQlExtractor._extract_with_regex_fallback` | `ApiSpecInfo` | `codd/parsing.py:1518` | no |
| `ProtobufExtractor.extract_services` | `ApiSpecInfo` | `codd/parsing.py:1557` | no |
| `DockerComposeExtractor.extract_services` | `ConfigInfo` | `codd/parsing.py:1622` | no |
| `KubernetesExtractor.extract_manifests` | `ConfigInfo` | `codd/parsing.py:1668` | no |
| `TerraformExtractor.extract_resources` | `ConfigInfo` | `codd/parsing.py:1763` | no |
| `TerraformExtractor._extract_resources_regex` | `ConfigInfo` | `codd/parsing.py:1832` | no |
| `BuildDepsExtractor.extract_deps` | `BuildDepsInfo` | `codd/parsing.py:1872` | no |
| `BuildDepsExtractor.merge` | `BuildDepsInfo` | `codd/parsing.py:1880` | no |
| `BuildDepsExtractor._extract_pyproject` | `BuildDepsInfo` | `codd/parsing.py:1896` | no |
| `BuildDepsExtractor._extract_pyproject` | `BuildDepsInfo` | `codd/parsing.py:1901` | no |
| `BuildDepsExtractor._extract_pyproject` | `BuildDepsInfo` | `codd/parsing.py:1914` | no |
| `BuildDepsExtractor._extract_package_json` | `BuildDepsInfo` | `codd/parsing.py:1925` | no |
| `BuildDepsExtractor._extract_package_json` | `BuildDepsInfo` | `codd/parsing.py:1927` | no |
| `BuildDepsExtractor._extract_go_mod` | `BuildDepsInfo` | `codd/parsing.py:1962` | no |
| `TestExtractor.extract_test_info` | `TestInfo` | `codd/parsing.py:2002` | no |
| `TestExtractor._extract_python` | `TestInfo` | `codd/parsing.py:2048` | no |
| `TestExtractor._extract_javascript` | `TestInfo` | `codd/parsing.py:2053` | no |
| `TestExtractor._extract_go` | `TestInfo` | `codd/parsing.py:2058` | no |
| `get_extractor` | `is_available` | `codd/parsing.py:2071` | no |
| `get_extractor` | `SqlDdlExtractor` | `codd/parsing.py:2072` | no |
| `get_extractor` | `RegexExtractor` | `codd/parsing.py:2073` | no |
| `get_extractor` | `PrismaSchemaExtractor` | `codd/parsing.py:2075` | no |
| `get_extractor` | `RegexExtractor` | `codd/parsing.py:2076` | no |
| `get_extractor` | `is_available` | `codd/parsing.py:2081` | no |
| `get_extractor` | `TreeSitterExtractor` | `codd/parsing.py:2083` | no |
| `get_extractor` | `RegexExtractor` | `codd/parsing.py:2085` | no |

## Test Coverage

**Coverage**: 0.1 (4 / 42)
Tests: tests/test_parsing.py

**Uncovered symbols**: `ApiSpecInfo`, `BuildDepsExtractor`, `BuildDepsInfo`, `ConfigInfo`, `DockerComposeExtractor`, `GraphQlExtractor`, `KubernetesExtractor`, `LanguageExtractor`, `OpenApiExtractor`, `PrismaSchemaExtractor`, `PrismaSchemaInfo`, `ProtobufExtractor`, `SqlDdlExtractor`, `SqlSchemaInfo`, `TerraformExtractor`, `TestExtractor`, `TestInfo`, `detect_build_files`, `detect_code_patterns`, `detect_docker_compose`, `detect_graphql_files`, `detect_k8s_manifests`, `detect_openapi_files`, `detect_proto_files`, `detect_test_files`, `detect_tf_files`, `extract_call_graph`, `extract_deps`, `extract_endpoints`, `extract_imports`, `extract_manifests`, `extract_resources`, `extract_schema`, `extract_services`, `extract_symbols`, `extract_test_info`, `merge`, `visit`




## Import Dependencies

### → extractor

- `from codd.extractor import CallEdge, ModuleInfo, Symbol`
- `from codd.extractor import Symbol`
- `from codd.extractor import CallEdge`
- `from codd.extractor import CallEdge`

## External Dependencies

- `ast`
- `codd`
- `graphql`
- `hcl2`
- `tomli as tomllib`
- `tomllib`
- `tree_sitter`
- `tree_sitter_python`
- `tree_sitter_sql`
- `tree_sitter_typescript`
- `yaml`

## Files

- `codd/parsing.py`

## Tests

- `tests/test_parsing.py` — tests: test_symbol_defaults_are_non_breaking, test_projectfacts_defaults_are_non_breaking, test_get_extractor_returns_regex_for_unsupported_language, test_get_extractor_returns_tree_sitter_when_available, test_get_extractor_returns_regex_when_tree_sitter_missing