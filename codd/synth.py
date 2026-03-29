"""Template-based Markdown synthesis for extracted CoDD facts."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from codd.extractor import ModuleInfo, ProjectFacts, Symbol


_TEMPLATE_DIR = Path(__file__).parent / "templates" / "extracted"
_LAYER_ORDER = ["Presentation", "Application", "Domain", "Infrastructure", "Shared"]
_LAYER_RANK = {
    "Shared": 0,
    "Infrastructure": 1,
    "Domain": 2,
    "Application": 3,
    "Presentation": 4,
}

_PRESENTATION_KEYWORDS = {"api", "route", "routes", "controller", "handler", "cli", "server"}
_APPLICATION_KEYWORDS = {"service", "usecase", "planner", "generator", "implementer", "verifier", "auth"}
_DOMAIN_KEYWORDS = {"domain", "model", "models", "entity", "entities", "db", "schema", "schemas"}
_INFRASTRUCTURE_KEYWORDS = {
    "extractor",
    "parsing",
    "parser",
    "scanner",
    "graph",
    "config",
    "hooks",
    "validator",
    "propagate",
    "adapter",
    "client",
    "repository",
    "repos",
    "store",
}
_SHARED_KEYWORDS = {"util", "utils", "shared", "common", "helper", "helpers", "types"}

_CONCERN_KEYWORDS = {
    "Authentication & Authorization": {"auth", "jwt", "oauth", "permission", "rbac", "acl"},
    "Logging & Observability": {"log", "logging", "logger", "trace", "metrics"},
    "Configuration": {"config", "settings", "env", "dotenv"},
    "Error Handling": {"error", "errors", "exception", "retry", "fallback"},
}


def synth_docs(facts: ProjectFacts, output_dir: Path) -> list[Path]:
    """Generate extracted design documents from project facts."""
    env = _build_environment()
    today = date.today().isoformat()

    output_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = output_dir / "modules"
    schemas_dir = output_dir / "schemas"
    api_dir = output_dir / "api"
    modules_dir.mkdir(exist_ok=True)
    schemas_dir.mkdir(exist_ok=True)
    api_dir.mkdir(exist_ok=True)

    generated: list[Path] = []

    system_context_path = output_dir / "system-context.md"
    system_context_path.write_text(
        _render_system_context(env, facts, today),
        encoding="utf-8",
    )
    generated.append(system_context_path)

    for module_name, module in sorted(facts.modules.items()):
        if not module.files:
            continue
        module_path = modules_dir / f"{_slugify(module_name)}.md"
        module_path.write_text(
            _render_module_detail(env, facts, module, today),
            encoding="utf-8",
        )
        generated.append(module_path)

    for relative_path, schema in sorted(facts.schemas.items()):
        schema_path = schemas_dir / f"{_slugify(Path(relative_path).with_suffix('').as_posix())}.md"
        schema_path.write_text(
            _render_schema_design(env, relative_path, schema, today),
            encoding="utf-8",
        )
        generated.append(schema_path)

    for relative_path, spec in sorted(facts.api_specs.items()):
        api_path = api_dir / f"{_slugify(Path(relative_path).with_suffix('').as_posix())}.md"
        api_path.write_text(
            _render_api_contract(env, relative_path, spec, today),
            encoding="utf-8",
        )
        generated.append(api_path)

    architecture_path = synth_architecture(facts, output_dir, env=env, today=today)
    generated.append(architecture_path)

    return generated


def synth_architecture(
    facts: ProjectFacts,
    output_dir: Path,
    *,
    env: Environment | None = None,
    today: str | None = None,
) -> Path:
    """Generate an architecture overview document from project facts."""
    jinja_env = env or _build_environment()
    rendered_today = today or date.today().isoformat()
    reverse_dependencies = _reverse_dependencies(facts)
    layer_map = _classify_modules_to_layers(facts, reverse_dependencies)
    violations = _detect_layer_violations(facts, layer_map)
    concerns = _detect_cross_cutting_concerns(facts)
    dependency_lines = _dependency_lines(facts)
    architecture_path = output_dir / "architecture-overview.md"
    frontmatter = _build_frontmatter(
        node_id="design:extract:architecture-overview",
        confidence=_architecture_confidence(facts, violations),
        today=rendered_today,
        depends_on=[
            {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"},
            *[
                {"id": _module_node_id(module_name), "relation": "aggregates", "semantic": "technical"}
                for module_name in sorted(facts.modules)
                if facts.modules[module_name].files
            ],
            *[
                {"id": _schema_node_id(relative_path), "relation": "aggregates", "semantic": "technical"}
                for relative_path in sorted(facts.schemas)
            ],
            *[
                {"id": _api_node_id(relative_path), "relation": "aggregates", "semantic": "technical"}
                for relative_path in sorted(facts.api_specs)
            ],
        ],
    )

    content = jinja_env.get_template("architecture-overview.md.j2").render(
        frontmatter=frontmatter,
        facts=facts,
        frameworks=_dedupe_strings(facts.detected_frameworks),
        layers=[
            {
                "name": layer_name,
                "modules": layer_map[layer_name],
            }
            for layer_name in _LAYER_ORDER
        ],
        violations=violations,
        dependency_lines=dependency_lines,
        feature_clusters=facts.feature_clusters,
        interface_contracts=_interface_contracts_summary(facts),
        schema_rows=_schema_summary_rows(facts),
        api_rows=_api_summary_rows(facts),
        infra_rows=_infra_summary_rows(facts),
        concerns=concerns,
        external_dependencies=_all_external_dependencies(facts),
        build_deps=_build_deps_context(facts.build_deps),
        deployment_hints=_deployment_hints(facts),
    )
    architecture_path.write_text(content, encoding="utf-8")
    return architecture_path


def _build_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_system_context(env: Environment, facts: ProjectFacts, today: str) -> str:
    module_rows = []
    for module_name, module in sorted(facts.modules.items()):
        route_paths = _route_paths_for_module(module)
        module_rows.append(
            {
                "name": module_name,
                "files": len(module.files),
                "line_count": module.line_count,
                "class_count": sum(1 for symbol in module.symbols if symbol.kind == "class"),
                "function_count": sum(1 for symbol in module.symbols if symbol.kind == "function"),
                "async_count": sum(
                    1 for symbol in module.symbols if symbol.kind == "function" and symbol.is_async
                ),
                "type_count": sum(
                    1 for symbol in module.symbols if symbol.kind in {"interface", "type_alias", "enum"}
                ),
                "dependencies": ", ".join(
                    dependency
                    for dependency in sorted(module.internal_imports)
                    if dependency in facts.modules
                )
                or "—",
                "routes": ", ".join(route_paths) or "—",
                "patterns": "; ".join(module.patterns.values()) or "—",
            }
        )

    content = env.get_template("system-context.md.j2").render(
        frontmatter=_build_frontmatter(
            node_id="design:extract:system-context",
            confidence=0.65,
            today=today,
        ),
        facts=facts,
        frameworks=_dedupe_strings(facts.detected_frameworks),
        module_rows=module_rows,
        dependency_lines=_dependency_lines(facts),
        external_dependencies=_all_external_dependencies(facts),
        schema_rows=_schema_summary_rows(facts),
        api_rows=_api_summary_rows(facts),
        infra_rows=_infra_summary_rows(facts),
        build_deps=_build_deps_context(facts.build_deps),
    )
    return content


def _render_module_detail(env: Environment, facts: ProjectFacts, module: ModuleInfo, today: str) -> str:
    reverse_dependencies = _reverse_dependencies(facts)
    layer_name, layer_reason = _classify_module(facts, module.name, module, reverse_dependencies)
    route_paths = _route_paths_for_module(module)
    related_schemas = _related_schemas(facts, module)
    related_api_specs = _related_api_specs(facts, module, route_paths)

    content = env.get_template("module-detail.md.j2").render(
        frontmatter=_build_frontmatter(
            node_id=_module_node_id(module.name),
            confidence=_module_confidence(module),
            today=today,
            depends_on=_module_depends_on(facts, module),
        ),
        mod=module,
        layer_name=layer_name,
        layer_reason=layer_reason,
        route_paths=route_paths,
        classes=[symbol for symbol in module.symbols if symbol.kind == "class"],
        functions=[symbol for symbol in module.symbols if symbol.kind == "function"],
        type_definitions=[symbol for symbol in module.symbols if symbol.kind in {"interface", "type_alias", "enum"}],
        async_functions=[
            symbol for symbol in module.symbols if symbol.kind == "function" and symbol.is_async
        ],
        internal_dependencies=[
            {
                "name": dependency_name,
                "imports": import_lines[:5],
                "extra_count": max(len(import_lines) - 5, 0),
            }
            for dependency_name, import_lines in sorted(module.internal_imports.items())
        ],
        related_schemas=related_schemas,
        related_api_specs=related_api_specs,
        files=sorted(module.files),
        tests=_tests_context(module),
        call_edges=module.call_edges,
        interface_contract=module.interface_contract,
    )
    return content


def _render_schema_design(env: Environment, relative_path: str, schema: Any, today: str) -> str:
    content = env.get_template("schema-design.md.j2").render(
        frontmatter=_build_frontmatter(
            node_id=_schema_node_id(relative_path),
            confidence=_schema_confidence(schema),
            today=today,
        ),
        relative_path=relative_path,
        slug=_slugify(Path(relative_path).with_suffix("").as_posix()),
        tables=getattr(schema, "tables", []),
        foreign_keys=getattr(schema, "foreign_keys", []),
        indexes=getattr(schema, "indexes", []),
        views=getattr(schema, "views", []),
        prisma_models=getattr(schema, "models", []),
        prisma_enums=getattr(schema, "enums", []),
    )
    return content


def _render_api_contract(env: Environment, relative_path: str, spec: Any, today: str) -> str:
    graphql_ops = [endpoint for endpoint in spec.endpoints if endpoint.get("operation_type") in {"query", "mutation", "subscription"}]
    http_endpoints = [endpoint for endpoint in spec.endpoints if endpoint.get("path")]
    rpc_endpoints = [endpoint for endpoint in spec.endpoints if endpoint.get("operation_type") == "rpc"]
    content = env.get_template("api-contract.md.j2").render(
        frontmatter=_build_frontmatter(
            node_id=_api_node_id(relative_path),
            confidence=_api_confidence(spec),
            today=today,
        ),
        relative_path=relative_path,
        spec=spec,
        http_endpoints=http_endpoints,
        graphql_ops=graphql_ops,
        rpc_endpoints=rpc_endpoints,
    )
    return content


def _build_frontmatter(
    *,
    node_id: str,
    confidence: float,
    today: str,
    depends_on: list[dict[str, Any]] | None = None,
) -> str:
    codd: dict[str, Any] = {
        "node_id": node_id,
        "type": "design",
        "source": "extracted",
        "confidence": round(confidence, 2),
        "last_extracted": today,
    }
    if depends_on:
        codd["depends_on"] = depends_on
    payload = yaml.safe_dump({"codd": codd}, sort_keys=False, allow_unicode=True)
    return payload


def _module_depends_on(facts: ProjectFacts, module: ModuleInfo) -> list[dict[str, Any]]:
    depends_on = []
    seen_ids: set[str] = set()
    for dependency_name in sorted(module.internal_imports):
        if dependency_name not in facts.modules:
            continue
        nid = _module_node_id(dependency_name)
        if nid not in seen_ids:
            depends_on.append(
                {"id": nid, "relation": "imports", "semantic": "technical"}
            )
            seen_ids.add(nid)

    # R4.1: call-graph edges
    call_targets: set[str] = set()
    for edge in module.call_edges:
        target_mod = edge.callee.split(".")[0]
        if target_mod in facts.modules and target_mod != module.name:
            call_targets.add(target_mod)
    for target in sorted(call_targets):
        nid = _module_node_id(target)
        if nid not in seen_ids:
            depends_on.append(
                {"id": nid, "relation": "calls", "semantic": "technical"}
            )
            seen_ids.add(nid)

    # R4.2: co-feature edges
    for cluster in facts.feature_clusters:
        if module.name in cluster.modules:
            for peer in cluster.modules:
                if peer != module.name:
                    nid = _module_node_id(peer)
                    if nid not in seen_ids:
                        depends_on.append(
                            {"id": nid, "relation": "co_feature", "semantic": "technical"}
                        )
                        seen_ids.add(nid)

    return depends_on


def _module_node_id(module_name: str) -> str:
    return f"design:extract:{_slugify(module_name)}"


def _schema_node_id(relative_path: str) -> str:
    return f"design:extract:schema-{_slugify(Path(relative_path).with_suffix('').as_posix())}"


def _api_node_id(relative_path: str) -> str:
    return f"design:extract:api-{_slugify(Path(relative_path).with_suffix('').as_posix())}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "root"


def _module_confidence(module: ModuleInfo) -> float:
    score = 0.5
    if len(module.files) >= 3:
        score += 0.1
    if module.symbols:
        score += 0.1
    if module.test_files:
        score += 0.1
    if module.patterns:
        score += 0.05
    if any(
        symbol.decorators
        or symbol.return_type
        or symbol.is_async
        or symbol.bases
        or symbol.implements
        or symbol.kind in {"interface", "type_alias", "enum"}
        for symbol in module.symbols
    ):
        score += 0.05
    return min(score, 0.85)


def _schema_confidence(schema: Any) -> float:
    score = 0.6
    if getattr(schema, "tables", None) or getattr(schema, "models", None):
        score += 0.1
    if getattr(schema, "foreign_keys", None) or _prisma_relations(schema):
        score += 0.1
    if getattr(schema, "indexes", None) or getattr(schema, "enums", None):
        score += 0.05
    return min(score, 0.85)


def _api_confidence(spec: Any) -> float:
    score = 0.6
    if spec.endpoints:
        score += 0.1
    if spec.schemas:
        score += 0.1
    if spec.services:
        score += 0.05
    return min(score, 0.85)


def _architecture_confidence(facts: ProjectFacts, violations: list[dict[str, Any]]) -> float:
    score = 0.6
    if _dependency_lines(facts):
        score += 0.05
    if facts.schemas:
        score += 0.05
    if facts.api_specs:
        score += 0.05
    if facts.infra_config:
        score += 0.05
    if violations:
        score += 0.05
    return min(score, 0.85)


def _route_paths_for_module(module: ModuleInfo) -> list[str]:
    routes = set()
    for symbol in module.symbols:
        for decorator in symbol.decorators:
            match = re.search(
                r'(?:^|\.)\s*(?:route|get|post|put|delete|patch)\s*\(\s*(?:path\s*=\s*)?["\']([^"\']+)["\']',
                decorator.replace("\n", " "),
            )
            if match:
                routes.add(match.group(1))

    pattern_value = module.patterns.get("api_routes", "")
    for route in re.findall(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", pattern_value):
        routes.add(route)

    return sorted(routes)


def _schema_summary_rows(facts: ProjectFacts) -> list[dict[str, Any]]:
    rows = []
    for relative_path, schema in sorted(facts.schemas.items()):
        rows.append(
            {
                "title": relative_path,
                "format": _schema_format(schema),
                "node_id": _schema_node_id(relative_path),
                "table_count": len(getattr(schema, "tables", [])),
                "model_count": len(getattr(schema, "models", [])),
                "foreign_key_count": len(getattr(schema, "foreign_keys", [])),
                "index_count": len(getattr(schema, "indexes", [])),
                "enum_count": len(getattr(schema, "enums", [])),
            }
        )
    return rows


def _api_summary_rows(facts: ProjectFacts) -> list[dict[str, Any]]:
    rows = []
    for relative_path, spec in sorted(facts.api_specs.items()):
        rows.append(
            {
                "title": relative_path,
                "format": spec.format,
                "node_id": _api_node_id(relative_path),
                "endpoint_count": len(spec.endpoints),
                "schema_count": len(spec.schemas),
                "service_count": len(spec.services),
            }
        )
    return rows


def _infra_summary_rows(facts: ProjectFacts) -> list[dict[str, Any]]:
    rows = []
    for relative_path, config in sorted(facts.infra_config.items()):
        rows.append(
            {
                "title": relative_path,
                "format": config.format,
                "service_count": len(config.services),
                "resource_count": len(config.resources),
            }
        )
    return rows


def _build_deps_context(build_deps: Any) -> dict[str, Any] | None:
    if build_deps is None:
        return None
    return {
        "file_path": build_deps.file_path,
        "runtime": list(build_deps.runtime),
        "dev": list(build_deps.dev),
        "scripts": dict(build_deps.scripts),
    }


def _all_external_dependencies(facts: ProjectFacts) -> list[str]:
    dependencies = set()
    for module in facts.modules.values():
        dependencies.update(module.external_imports)
    if facts.build_deps is not None:
        dependencies.update(facts.build_deps.runtime)
        dependencies.update(facts.build_deps.dev)
    return sorted(dependencies)


def _interface_contracts_summary(facts: ProjectFacts) -> list[dict[str, Any]]:
    """Build template-friendly interface contract rows."""
    rows: list[dict[str, Any]] = []
    for mod in facts.modules.values():
        ic = mod.interface_contract
        if ic is None:
            continue
        rows.append({
            "module": ic.module,
            "public_count": len(ic.public_symbols),
            "internal_count": len(ic.internal_symbols),
            "ratio": ic.api_surface_ratio,
            "violations": ic.encapsulation_violations,
        })
    return sorted(rows, key=lambda r: r["module"])


def _dependency_lines(facts: ProjectFacts) -> list[str]:
    lines = []
    for module_name, module in sorted(facts.modules.items()):
        for dependency_name in sorted(module.internal_imports):
            if dependency_name in facts.modules:
                lines.append(f"{module_name} -> {dependency_name}")
    return lines


def _reverse_dependencies(facts: ProjectFacts) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {module_name: set() for module_name in facts.modules}
    for module_name, module in facts.modules.items():
        for dependency_name in module.internal_imports:
            if dependency_name in facts.modules:
                reverse.setdefault(dependency_name, set()).add(module_name)
    return reverse


def _classify_modules_to_layers(
    facts: ProjectFacts,
    reverse_dependencies: dict[str, set[str]],
) -> dict[str, list[dict[str, Any]]]:
    layers = {layer_name: [] for layer_name in _LAYER_ORDER}
    for module_name, module in sorted(facts.modules.items()):
        layer_name, reason = _classify_module(facts, module_name, module, reverse_dependencies)
        layers[layer_name].append(
            {
                "name": module_name,
                "reason": reason,
                "routes": _route_paths_for_module(module),
                "patterns": list(module.patterns.values()),
                "files": sorted(module.files),
            }
        )
    return layers


def _classify_module(
    facts: ProjectFacts,
    module_name: str,
    module: ModuleInfo,
    reverse_dependencies: dict[str, set[str]],
) -> tuple[str, str]:
    route_paths = _route_paths_for_module(module)
    lowered_name = module_name.lower()

    if route_paths or _contains_keyword(lowered_name, _PRESENTATION_KEYWORDS):
        return "Presentation", "Exposes routes, handlers, or CLI entrypoints"

    if _contains_keyword(lowered_name, _SHARED_KEYWORDS):
        return "Shared", "Provides shared helpers or reusable types"

    if "db_models" in module.patterns or _contains_keyword(lowered_name, _DOMAIN_KEYWORDS):
        return "Domain", "Owns schema, persistence, or domain model concepts"

    if _contains_keyword(lowered_name, _INFRASTRUCTURE_KEYWORDS):
        return "Infrastructure", "Implements parsing, extraction, scanning, or adapters"

    if _contains_keyword(lowered_name, _APPLICATION_KEYWORDS):
        return "Application", "Coordinates use cases or service-level workflows"

    if any(
        _route_paths_for_module(facts.modules[parent_name])
        or _contains_keyword(parent_name.lower(), _PRESENTATION_KEYWORDS)
        for parent_name in reverse_dependencies.get(module_name, set())
        if parent_name in facts.modules
    ):
        return "Application", "Used directly by presentation-facing modules"

    if any(
        dependency_name in facts.modules
        and (
            "db_models" in facts.modules[dependency_name].patterns
            or _contains_keyword(dependency_name.lower(), _DOMAIN_KEYWORDS)
        )
        for dependency_name in module.internal_imports
    ):
        return "Application", "Orchestrates modules that own domain or schema concerns"

    return "Infrastructure", "Defaulted to infrastructure because no higher-level cues were detected"


def _detect_layer_violations(
    facts: ProjectFacts,
    layers: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    assignment = {
        module["name"]: layer_name
        for layer_name, modules in layers.items()
        for module in modules
    }
    violations = []
    for source_name, module in sorted(facts.modules.items()):
        source_layer = assignment.get(source_name)
        if source_layer is None:
            continue
        for target_name, import_lines in sorted(module.internal_imports.items()):
            target_layer = assignment.get(target_name)
            if target_layer is None:
                continue
            if _LAYER_RANK[source_layer] < _LAYER_RANK[target_layer]:
                violations.append(
                    {
                        "source": source_name,
                        "source_layer": source_layer,
                        "target": target_name,
                        "target_layer": target_layer,
                        "imports": import_lines[:3],
                    }
                )
    return violations


def _detect_cross_cutting_concerns(facts: ProjectFacts) -> list[dict[str, Any]]:
    concerns = {concern_name: set() for concern_name in _CONCERN_KEYWORDS}
    for module_name, module in facts.modules.items():
        haystack_parts = [
            module_name.lower(),
            " ".join(path.lower() for path in module.files),
            " ".join(module.external_imports).lower(),
            " ".join(module.patterns.values()).lower(),
        ]
        haystack = " ".join(haystack_parts)
        for concern_name, keywords in _CONCERN_KEYWORDS.items():
            if any(keyword in haystack for keyword in keywords):
                concerns[concern_name].add(module_name)

    return [
        {"name": concern_name, "modules": sorted(module_names)}
        for concern_name, module_names in concerns.items()
        if module_names
    ]


def _related_schemas(facts: ProjectFacts, module: ModuleInfo) -> list[dict[str, Any]]:
    model_names = _model_names_for_module(module)
    related = []
    for relative_path, schema in sorted(facts.schemas.items()):
        matches = _schema_matches(schema, model_names)
        if not matches:
            continue
        related.append(
            {
                "title": relative_path,
                "node_id": _schema_node_id(relative_path),
                "matches": matches,
            }
        )
    return related


def _related_api_specs(
    facts: ProjectFacts,
    module: ModuleInfo,
    route_paths: list[str],
) -> list[dict[str, Any]]:
    related = []
    module_name = module.name.lower()
    for relative_path, spec in sorted(facts.api_specs.items()):
        matching_paths = []
        for endpoint in spec.endpoints:
            endpoint_path = endpoint.get("path")
            if endpoint_path and endpoint_path in route_paths:
                matching_paths.append(f"{endpoint.get('method', endpoint.get('operation_type', '')).strip()} {endpoint_path}".strip())

        if not matching_paths and module_name not in relative_path.lower():
            continue

        related.append(
            {
                "title": relative_path,
                "node_id": _api_node_id(relative_path),
                "matches": matching_paths,
            }
        )
    return related


def _model_names_for_module(module: ModuleInfo) -> set[str]:
    names = {
        symbol.name
        for symbol in module.symbols
        if symbol.kind == "class"
    }
    pattern_value = module.patterns.get("db_models", "")
    if ":" in pattern_value:
        _, _, tail = pattern_value.partition(":")
        names.update(part.strip() for part in tail.split(",") if part.strip())
    return names


def _schema_matches(schema: Any, model_names: set[str]) -> list[str]:
    if not model_names:
        return []

    matched = []
    normalized_models = {
        variant
        for model_name in model_names
        for variant in _name_variants(model_name)
    }

    for table in getattr(schema, "tables", []):
        if normalized_models & _name_variants(table.get("name", "")):
            matched.append(table.get("name", ""))

    for model in getattr(schema, "models", []):
        if normalized_models & _name_variants(model.get("name", "")):
            matched.append(model.get("name", ""))

    return sorted(set(name for name in matched if name))


def _tests_context(module: ModuleInfo) -> list[dict[str, Any]]:
    if module.test_details:
        return [
            {
                "file_path": test_info.file_path,
                "test_functions": list(test_info.test_functions),
                "fixtures": list(test_info.fixtures),
            }
            for test_info in module.test_details
        ]

    return [
        {
            "file_path": file_path,
            "test_functions": [],
            "fixtures": [],
        }
        for file_path in sorted(module.test_files)
    ]


def _deployment_hints(facts: ProjectFacts) -> list[str]:
    hints = []
    if facts.entry_points:
        hints.append(f"Entry points: {', '.join(facts.entry_points)}")
    if any(config.format == "docker-compose" for config in facts.infra_config.values()):
        hints.append("Docker Compose manifests detected")
    if any(config.format == "kubernetes" for config in facts.infra_config.values()):
        hints.append("Kubernetes manifests detected")
    if facts.build_deps and facts.build_deps.scripts:
        hints.append(f"Build scripts: {', '.join(sorted(facts.build_deps.scripts))}")
    return hints


def _schema_format(schema: Any) -> str:
    if getattr(schema, "models", None) or getattr(schema, "enums", None):
        return "prisma"
    return "sql"


def _prisma_relations(schema: Any) -> list[dict[str, Any]]:
    relations = []
    for model in getattr(schema, "models", []):
        relations.extend(model.get("relations", []))
    return relations


def _name_variants(value: str) -> set[str]:
    base = re.sub(r"[^a-z0-9]", "", value.lower())
    variants = {base}
    if base.endswith("s"):
        variants.add(base[:-1])
    elif base:
        variants.add(f"{base}s")
    return {variant for variant in variants if variant}


def _contains_keyword(value: str, keywords: set[str]) -> bool:
    parts = set(filter(None, re.split(r"[^a-z0-9]+", value)))
    return any(keyword in parts for keyword in keywords)


def _dedupe_strings(values: list[str]) -> list[str]:
    return sorted(dict.fromkeys(values))
