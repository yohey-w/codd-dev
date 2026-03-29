---
codd:
  node_id: design:extract:parsing
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# parsing

> 1 files, 2,136 lines

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
| class | `RegexExtractor` | `codd/parsing.py:171` | — |
| class | `TreeSitterExtractor` | `codd/parsing.py:218` | — |
| class | `SqlDdlExtractor` | `codd/parsing.py:295` | — |
| class | `PrismaSchemaExtractor` | `codd/parsing.py:334` | — |
| class | `OpenApiExtractor` | `codd/parsing.py:1148` | — |
| class | `GraphQlExtractor` | `codd/parsing.py:1240` | — |
| class | `ProtobufExtractor` | `codd/parsing.py:1356` | — |
| class | `DockerComposeExtractor` | `codd/parsing.py:1408` | — |
| class | `KubernetesExtractor` | `codd/parsing.py:1458` | — |
| class | `TerraformExtractor` | `codd/parsing.py:1550` | — |
| class | `BuildDepsExtractor` | `codd/parsing.py:1664` | — |
| class | `TestExtractor` | `codd/parsing.py:1778` | — |
| function | `extract_symbols` | `codd/parsing.py:152` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:155` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:164` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:167` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `extract_symbols` | `codd/parsing.py:178` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:183` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:200` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:206` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `is_available` | `codd/parsing.py:228` | `is_available(cls, language: str | None = None) -> bool` |
| function | `extract_symbols` | `codd/parsing.py:239` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:252` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:271` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:288` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None` |
| function | `is_available` | `codd/parsing.py:306` | `is_available(cls) -> bool` |
| function | `extract_symbols` | `codd/parsing.py:309` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:312` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:321` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:324` | `extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | None` |
| function | `extract_symbols` | `codd/parsing.py:340` | `extract_symbols(self, content: str, file_path: str) -> list[Symbol]` |
| function | `extract_imports` | `codd/parsing.py:343` | `extract_imports(self, content: str, file_path: Path, project_root: Path, src_dir: Path,) -> tuple[dict[str, list[str]], set[str]]` |
| function | `detect_code_patterns` | `codd/parsing.py:352` | `detect_code_patterns(self, mod: ModuleInfo, content: str) -> None` |
| function | `extract_schema` | `codd/parsing.py:355` | `extract_schema(self, content: str, file_path: str | Path) -> PrismaSchemaInfo | None` |
| function | `visit` | `codd/parsing.py:586` | `visit(node: Any, decorators: list[str] | None = None)` |
| function | `visit` | `codd/parsing.py:707` | `visit(node: Any)` |
| function | `visit` | `codd/parsing.py:852` | `visit(node: Any)` |
| function | `detect_openapi_files` | `codd/parsing.py:1153` | `detect_openapi_files(self, project_root: Path) -> list[Path]` |
| function | `extract_endpoints` | `codd/parsing.py:1165` | `extract_endpoints(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_graphql_files` | `codd/parsing.py:1245` | `detect_graphql_files(self, project_root: Path) -> list[Path]` |
| function | `extract_schema` | `codd/parsing.py:1248` | `extract_schema(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_proto_files` | `codd/parsing.py:1361` | `detect_proto_files(self, project_root: Path) -> list[Path]` |
| function | `extract_services` | `codd/parsing.py:1364` | `extract_services(self, content: str, file_path: str) -> ApiSpecInfo` |
| function | `detect_docker_compose` | `codd/parsing.py:1421` | `detect_docker_compose(self, project_root: Path) -> list[Path]` |
| function | `extract_services` | `codd/parsing.py:1428` | `extract_services(self, content: str, file_path: str) -> ConfigInfo` |
| function | `detect_k8s_manifests` | `codd/parsing.py:1464` | `detect_k8s_manifests(self, project_root: Path) -> list[Path]` |
| function | `extract_manifests` | `codd/parsing.py:1475` | `extract_manifests(self, content: str, file_path: str) -> ConfigInfo` |
| function | `is_available` | `codd/parsing.py:1564` | `is_available(cls) -> bool` |
| function | `detect_tf_files` | `codd/parsing.py:1567` | `detect_tf_files(self, project_root: Path) -> list[Path]` |
| function | `extract_resources` | `codd/parsing.py:1570` | `extract_resources(self, content: str, file_path: str) -> ConfigInfo` |
| function | `detect_build_files` | `codd/parsing.py:1669` | `detect_build_files(self, project_root: Path) -> list[Path]` |
| function | `extract_deps` | `codd/parsing.py:1672` | `extract_deps(self, content: str, file_type: str, file_path: str = "") -> BuildDepsInfo` |
| function | `merge` | `codd/parsing.py:1682` | `merge(self, infos: list[BuildDepsInfo]) -> BuildDepsInfo | None` |
| function | `detect_test_files` | `codd/parsing.py:1784` | `detect_test_files(self, project_root: Path) -> list[Path]` |
| function | `extract_test_info` | `codd/parsing.py:1800` | `extract_test_info(self, content: str, file_path: str) -> TestInfo` |
| function | `get_extractor` | `codd/parsing.py:1866` | `get_extractor(language: str, category: str = "source") -> LanguageExtractor` |






## Import Dependencies

### → extractor

- `from codd.extractor import ModuleInfo, Symbol`
- `from codd.extractor import Symbol`

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