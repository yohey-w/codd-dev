"""Extract deployment verification nodes and edges from project files."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from codd.config import find_codd_dir, load_project_config
from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
    DeploymentDocNode,
    RuntimeStateKind,
    RuntimeStateNode,
    VerificationKind,
    VerificationTestNode,
)


MARKDOWN_H2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
URL_PATH_RE = re.compile(r"(?<![:\w])/[A-Za-z0-9_./{}:-]+")

DEPLOYMENT_DOC_PATTERNS = ("DEPLOYMENT.md", "DEPLOY.md", "docs/deploy/*.md")
SMOKE_TEST_PATTERNS = ("tests/smoke/*.test.ts", "tests/smoke/*.spec.ts", "tests/smoke/*.sh")
E2E_TEST_PATTERNS = ("tests/e2e/*.spec.ts", "tests/e2e/*.test.ts")
RUNTIME_CAPABILITY_INFERENCE_DEFAULTS = Path(__file__).parent / "defaults" / "runtime_capability_inference.yaml"

DEPLOYMENT_SECTION_IMPL_PATTERNS: dict[str, tuple[str, ...]] = {
    "migrate": ("prisma/migrations/**/*", "schema.prisma", "**/migrations/**/*.sql"),
    "seed": ("prisma/seed.ts", "prisma/seed.js", "scripts/seed.ts", "scripts/seed.js"),
    "build": ("Dockerfile", "Containerfile", "package.json"),
    "start": (
        "main.ts", "main.js", "server.ts", "server.js",
        "app.ts", "app.js", "index.ts", "index.js",
        "src/main.ts", "src/main.js", "src/server.ts", "src/server.js",
    ),
}


def extract_deployment_nodes(project_root: Path, codd_config: dict[str, Any] | None = None) -> dict[str, list]:
    """Extract deployment document, runtime state, and verification test nodes."""

    root = Path(project_root).resolve()
    config = codd_config if codd_config is not None else _load_project_config_or_empty(root)
    deployment_docs = extract_deployment_docs(root, config)
    design_docs = _load_design_doc_records(root, config)
    runtime_states = extract_runtime_states(root, deployment_docs, design_docs, config)
    verification_tests = extract_verification_tests(root, config, design_docs)
    return {
        "deployment_docs": deployment_docs,
        "runtime_states": runtime_states,
        "verification_tests": verification_tests,
    }


def extract_deployment_docs(
    project_root: Path,
    codd_config: dict[str, Any] | None = None,
) -> list[DeploymentDocNode]:
    """Extract deployment documents from Markdown files and deploy.yaml."""

    root = Path(project_root).resolve()
    config = codd_config if codd_config is not None else _load_project_config_or_empty(root)
    deployment_config = _deployment_config(config)

    configured_documents = _configured_document_paths(deployment_config)
    if configured_documents:
        markdown_paths = [_project_path(root, document) for document in configured_documents]
    else:
        markdown_paths = _glob_paths(root, DEPLOYMENT_DOC_PATTERNS)

    nodes: dict[str, DeploymentDocNode] = {}
    for path in sorted(_existing_files(markdown_paths), key=lambda item: _relative_id(item, root)):
        frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        rel_path = _relative_id(path, root)
        nodes[rel_path] = DeploymentDocNode(
            path=rel_path,
            sections=_extract_markdown_sections(body),
            deploy_target_ref=_frontmatter_target_ref(frontmatter),
            depends_on=_as_str_list(frontmatter.get("depends_on")),
        )

    for path in _deploy_yaml_candidates(root):
        if not path.is_file():
            continue
        rel_path = _relative_id(path, root)
        payload = _read_yaml_mapping(path)
        nodes[rel_path] = DeploymentDocNode(
            path=rel_path,
            sections=_deploy_yaml_sections(payload),
            deploy_target_ref=_deploy_yaml_target_ref(payload),
            depends_on=_as_str_list(payload.get("depends_on")),
        )

    return [nodes[key] for key in sorted(nodes)]


def extract_runtime_states(
    project_root: Path,
    deployment_docs: list[DeploymentDocNode],
    design_docs: list,
    codd_config: dict[str, Any] | None = None,
) -> list[RuntimeStateNode]:
    """Infer runtime states from deployment sections and design acceptance criteria."""

    root = Path(project_root).resolve()
    config = codd_config if codd_config is not None else _load_project_config_or_empty(root)
    capabilities_provided = _infer_project_capabilities(root, config)
    states: dict[str, RuntimeStateNode] = {}
    for deployment_doc in deployment_docs:
        for section in deployment_doc.sections:
            kind_target = _runtime_state_for_section(section)
            if kind_target is None:
                continue
            kind, target, command = kind_target
            _add_runtime_state(
                states,
                RuntimeStateNode(
                    identifier=f"runtime:{kind.value}:{_slug(target)}",
                    kind=kind,
                    target=target,
                    expected_value=True,
                    actual_check_command=command,
                    capabilities_provided=capabilities_provided,
                ),
            )

    for design_doc in _iter_design_docs(design_docs):
        criteria = design_doc["criteria"]
        if _mentions_any(criteria, ("login", "user", "ログイン")):
            _add_runtime_state(
                states,
                RuntimeStateNode(
                    identifier="runtime:db_seed:users",
                    kind=RuntimeStateKind.DB_SEED,
                    target="users",
                    expected_value={"rows": ">=1"},
                    actual_check_command="seed users table",
                ),
            )

    return [states[key] for key in sorted(states)]


def infer_capabilities_provided(deploy_yaml: dict[str, Any], codd_yaml: dict[str, Any]) -> list[str]:
    """Infer runtime capabilities from declarative project rules."""

    facts = _deploy_capability_facts(deploy_yaml)
    capabilities: list[str] = []
    for rule in _runtime_capability_inference_rules(codd_yaml):
        if _capability_rule_matches(rule, facts):
            capabilities.extend(_as_str_list(rule.get("capabilities")))
    return _dedupe_strings(capabilities)


def extract_verification_tests(
    project_root: Path,
    codd_config: dict[str, Any] | None = None,
    design_docs: list | dict | None = None,
) -> list[VerificationTestNode]:
    """Extract smoke and E2E verification tests."""

    root = Path(project_root).resolve()
    config = codd_config if codd_config is not None else _load_project_config_or_empty(root)
    tests: dict[str, VerificationTestNode] = {}
    for path in _glob_paths(root, SMOKE_TEST_PATTERNS):
        _add_verification_test(tests, root, path, VerificationKind.SMOKE)
    for path in _glob_paths(root, E2E_TEST_PATTERNS):
        _add_verification_test(tests, root, path, VerificationKind.E2E)

    e2e_spec = root / "e2e-spec.md"
    if e2e_spec.is_file():
        rel_path = _relative_id(e2e_spec, root)
        tests[f"verification:e2e:{rel_path}"] = VerificationTestNode(
            identifier=f"verification:e2e:{rel_path}",
            kind=VerificationKind.E2E,
            target="acceptance_criteria",
            verification_template_ref="document",
            expected_outcome={"source": rel_path},
        )

    if _has_cdp_browser_template(config):
        records = design_docs if design_docs is not None else _load_design_doc_records(root, config)
        _add_cdp_browser_journey_tests(tests, records)

    return [tests[key] for key in sorted(tests)]


def discover_deployment_impl_candidates(
    project_root: Path,
    deployment_docs: list[DeploymentDocNode],
) -> list[Path]:
    """Return existing standard impl artifacts referenced by deployment doc sections.

    Walks each deployment document's sections and matches keywords against
    DEPLOYMENT_SECTION_IMPL_PATTERNS, then keeps the patterns that resolve to a
    real file under ``project_root``. The mapping itself stays inside the
    deployment plug-in so DAG core code can remain free of stack-specific
    filenames; this helper just runs an existence check.
    """

    root = Path(project_root)
    seen: dict[str, Path] = {}
    for doc in deployment_docs:
        for section in doc.sections or []:
            normalized = str(section).lower()
            for keyword, patterns in DEPLOYMENT_SECTION_IMPL_PATTERNS.items():
                if keyword not in normalized:
                    continue
                for pattern in patterns:
                    for path in root.glob(pattern):
                        if not path.is_file():
                            continue
                        rel = path.relative_to(root).as_posix()
                        if rel not in seen:
                            seen[rel] = path
    return list(seen.values())


def infer_deployment_edges(
    project_root: Path,
    deployment_docs: list[DeploymentDocNode],
    runtime_states: list[RuntimeStateNode],
    verification_tests: list[VerificationTestNode],
    impl_files: list,
    design_docs: list | dict | None = None,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Infer deployment verification edges.

    Returns:
        ``(from_id, to_id, edge_kind, attributes)`` tuples.
    """

    _ = project_root
    edges: list[tuple[str, str, str, dict[str, Any]]] = []
    impl_ids = [_impl_file_id(impl_file) for impl_file in _iter_impl_files(impl_files)]
    impl_ids = [impl_id for impl_id in impl_ids if impl_id]

    for deployment_doc in deployment_docs:
        for dependency in deployment_doc.depends_on:
            edges.append(
                (
                    _normalize_output_path(dependency),
                    deployment_doc.path,
                    EDGE_REQUIRES_DEPLOYMENT_STEP,
                    {"source": "deployment_frontmatter"},
                )
            )

    for design_doc in _iter_design_docs(design_docs or []):
        keywords = _deployment_keywords(design_doc["criteria"])
        if not keywords:
            continue
        for deployment_doc in deployment_docs:
            if not deployment_doc.sections or keywords.intersection(_deployment_keywords(" ".join(deployment_doc.sections))):
                edges.append(
                    (
                        design_doc["id"],
                        deployment_doc.path,
                        EDGE_REQUIRES_DEPLOYMENT_STEP,
                        {"source": "acceptance_criteria", "keywords": sorted(keywords)},
                    )
                )

    for deployment_doc in deployment_docs:
        for order, section in enumerate(deployment_doc.sections, start=1):
            for impl_id in _matching_impl_files(section, impl_ids):
                edges.append(
                    (
                        deployment_doc.path,
                        impl_id,
                        EDGE_EXECUTES_IN_ORDER,
                        {"order": order, "section": section},
                    )
                )

    for impl_id in impl_ids:
        produced_kind = _runtime_kind_for_impl(impl_id)
        if produced_kind is None:
            continue
        for runtime_state in runtime_states:
            if runtime_state.kind is produced_kind:
                edges.append(
                    (
                        impl_id,
                        runtime_state.identifier,
                        EDGE_PRODUCES_STATE,
                        {"source": "impl_path"},
                    )
                )

    for runtime_state in runtime_states:
        for verification_test in verification_tests:
            if _verification_matches_runtime(runtime_state, verification_test):
                edges.append(
                    (
                        runtime_state.identifier,
                        verification_test.identifier,
                        EDGE_VERIFIED_BY,
                        {"source": "target_match"},
                    )
                )

    return _dedupe_edges(edges)


def deployment_doc_attributes(node: DeploymentDocNode) -> dict[str, Any]:
    """Return DAG node attributes for a deployment document."""

    return asdict(node)


def runtime_state_attributes(node: RuntimeStateNode) -> dict[str, Any]:
    """Return JSON-serializable DAG node attributes for a runtime state."""

    payload = asdict(node)
    payload["kind"] = node.kind.value
    payload["capabilities_provided"] = _dedupe_strings(_as_str_list(payload.get("capabilities_provided")))
    return payload


def _infer_project_capabilities(project_root: Path, config: dict[str, Any]) -> list[str]:
    capabilities: list[str] = []
    for path in _deploy_yaml_candidates(project_root):
        if path.is_file():
            capabilities.extend(infer_capabilities_provided(_read_yaml_mapping(path), config))
    return _dedupe_strings(capabilities)


def _runtime_capability_inference_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    coherence = config.get("coherence")
    override = coherence.get("runtime_capability_inference") if isinstance(coherence, dict) else None
    if override is not None:
        return _coerce_inference_rules(override)
    return _coerce_inference_rules(_read_yaml_mapping(RUNTIME_CAPABILITY_INFERENCE_DEFAULTS))


def _coerce_inference_rules(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rules = payload.get("inference_rules")
    else:
        rules = payload
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if isinstance(rule, dict)]


def _deploy_capability_facts(deploy_yaml: dict[str, Any]) -> dict[str, set[str]]:
    target_types: set[str] = set()
    healthcheck_urls: set[str] = set()

    targets = _deploy_target_mappings(deploy_yaml)
    for target in targets:
        _add_text_fact(target_types, target.get("type"))
        healthcheck = target.get("healthcheck")
        if isinstance(healthcheck, dict):
            _add_text_fact(healthcheck_urls, healthcheck.get("url"))
        _add_text_fact(healthcheck_urls, target.get("url"))

    _add_text_fact(target_types, deploy_yaml.get("type"))
    healthcheck = deploy_yaml.get("healthcheck")
    if isinstance(healthcheck, dict):
        _add_text_fact(healthcheck_urls, healthcheck.get("url"))
    _add_text_fact(healthcheck_urls, deploy_yaml.get("url"))
    return {"target_types": target_types, "healthcheck_urls": healthcheck_urls}


def _deploy_target_mappings(deploy_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    targets = deploy_yaml.get("targets")
    if isinstance(targets, dict):
        return [target for target in targets.values() if isinstance(target, dict)]

    target = deploy_yaml.get("target")
    if isinstance(target, dict):
        return [target]
    return []


def _capability_rule_matches(rule: dict[str, Any], facts: dict[str, set[str]]) -> bool:
    matched = False

    target_type = rule.get("target_type")
    if isinstance(target_type, str):
        matched = True
        if target_type not in facts["target_types"]:
            return False

    url_prefix = rule.get("healthcheck_url_prefix")
    if isinstance(url_prefix, str):
        matched = True
        if not any(url.startswith(url_prefix) for url in facts["healthcheck_urls"]):
            return False

    return matched


def _add_text_fact(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip())


def verification_test_attributes(node: VerificationTestNode) -> dict[str, Any]:
    """Return JSON-serializable DAG node attributes for a verification test."""

    payload = asdict(node)
    payload["kind"] = node.kind.value
    payload["template_ref"] = node.verification_template_ref
    if isinstance(node.expected_outcome, dict):
        journey_name = node.expected_outcome.get("journey_name")
        if isinstance(journey_name, str) and journey_name:
            payload["journey_name"] = journey_name
    return payload


def _add_runtime_state(states: dict[str, RuntimeStateNode], state: RuntimeStateNode) -> None:
    states.setdefault(state.identifier, state)


def _add_verification_test(
    tests: dict[str, VerificationTestNode],
    project_root: Path,
    path: Path,
    kind: VerificationKind,
) -> None:
    if not path.is_file():
        return
    rel_path = _relative_id(path, project_root)
    identifier = f"verification:{kind.value}:{rel_path}"
    tests[identifier] = VerificationTestNode(
        identifier=identifier,
        kind=kind,
        target=_verification_target(path),
        verification_template_ref=_verification_template_ref(path),
        expected_outcome={"source": rel_path},
    )


def _add_cdp_browser_journey_tests(
    tests: dict[str, VerificationTestNode],
    design_docs: list | dict,
) -> None:
    for source_id, journey in _iter_design_doc_user_journeys(design_docs):
        journey_name = _journey_name(journey)
        if not journey_name:
            continue
        identifier = _unique_verification_identifier(tests, f"verification:cdp_browser:{journey_name}", source_id)
        tests[identifier] = VerificationTestNode(
            identifier=identifier,
            kind=VerificationKind.E2E,
            target=_verification_target_for_journey(journey, journey_name),
            verification_template_ref=_verification_template_ref(Path(journey_name), cdp_browser_journey=True),
            expected_outcome={
                "source": source_id,
                "journey_name": journey_name,
                "journey": journey,
            },
        )


def _deployment_config(config: dict[str, Any]) -> dict[str, Any]:
    deployment = config.get("deployment", {})
    return deployment if isinstance(deployment, dict) else {}


def _configured_document_paths(deployment_config: dict[str, Any]) -> list[str]:
    documents = deployment_config.get("documents")
    paths: list[str] = []
    for item in _as_list(documents):
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            value = item.get("path") or item.get("file")
            if isinstance(value, str):
                paths.append(value)
    return paths


def _deploy_yaml_candidates(project_root: Path) -> list[Path]:
    candidates = [project_root / "deploy.yaml"]
    codd_dir = find_codd_dir(project_root)
    if codd_dir is not None:
        candidates.append(codd_dir / "deploy.yaml")
    return _dedupe_paths(candidates)


def _deploy_yaml_sections(payload: dict[str, Any]) -> list[str]:
    sections: list[str] = []
    for step in _deploy_yaml_steps(payload):
        name = step.get("name") if isinstance(step, dict) else step
        if isinstance(name, str) and name.strip():
            sections.append(_section_key(name))
    return _dedupe_strings(sections)


def _deploy_yaml_steps(payload: dict[str, Any]) -> list[Any]:
    steps: list[Any] = []
    root_steps = payload.get("steps")
    if isinstance(root_steps, list):
        steps.extend(root_steps)

    targets = payload.get("targets")
    if isinstance(targets, dict):
        for target in targets.values():
            if isinstance(target, dict) and isinstance(target.get("steps"), list):
                steps.extend(target["steps"])
    return steps


def _deploy_yaml_target_ref(payload: dict[str, Any]) -> str | None:
    target_ref = payload.get("target") or payload.get("deploy_target_ref")
    if isinstance(target_ref, str) and target_ref:
        return target_ref
    targets = payload.get("targets")
    if isinstance(targets, dict) and len(targets) == 1:
        return next(iter(targets))
    return None


def _frontmatter_target_ref(frontmatter: dict[str, Any]) -> str | None:
    for key in ("deploy_target_ref", "deploy_target", "target"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_markdown_sections(body: str) -> list[str]:
    return [_section_key(match.group(1)) for match in MARKDOWN_H2_RE.finditer(body)]


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter_text = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            payload = yaml.safe_load(frontmatter_text) or {}
            return (payload if isinstance(payload, dict) else {}), body
    return {}, content


def _runtime_state_for_section(section: str) -> tuple[RuntimeStateKind, str, str] | None:
    normalized = section.lower()
    if "migrate" in normalized or "migration" in normalized:
        return RuntimeStateKind.DB_SCHEMA, "database_schema", "migration step"
    if "seed" in normalized:
        return RuntimeStateKind.DB_SEED, "seed_data", "seed step"
    if "start" in normalized or normalized in {"up", "server"} or "server" in normalized:
        return RuntimeStateKind.SERVER_RUNNING, "server", "start server"
    return None


def _runtime_kind_for_impl(impl_id: str) -> RuntimeStateKind | None:
    path = impl_id.lower()
    name = Path(path).name
    if "prisma/migrations/" in path or "/migrations/" in path or "migration" in name:
        return RuntimeStateKind.DB_SCHEMA
    if "seed" in name or "/seed" in path:
        return RuntimeStateKind.DB_SEED
    if name in {"main.ts", "main.js", "server.ts", "server.js", "app.ts", "app.js", "index.ts", "index.js"}:
        return RuntimeStateKind.SERVER_RUNNING
    return None


def _verification_matches_runtime(
    runtime_state: RuntimeStateNode,
    verification_test: VerificationTestNode,
) -> bool:
    target = verification_test.target.lower()
    state_target = runtime_state.target.lower()
    if state_target and state_target in target:
        return True
    if runtime_state.kind is RuntimeStateKind.DB_SEED:
        return _mentions_any(target, ("login", "user", "seed"))
    if runtime_state.kind is RuntimeStateKind.DB_SCHEMA:
        return _mentions_any(target, ("schema", "migration", "migrate", "database", "db"))
    if runtime_state.kind is RuntimeStateKind.SERVER_RUNNING:
        return _mentions_any(target, ("health", "server", "start", "running", "home"))
    return False


def _matching_impl_files(section: str, impl_ids: list[str]) -> list[str]:
    normalized = section.lower()
    matches: list[str] = []
    for impl_id in impl_ids:
        path = impl_id.lower()
        name = Path(path).name
        if ("migrate" in normalized or "migration" in normalized) and (
            "prisma/migrations/" in path or "/migrations/" in path or "schema.prisma" in path
        ):
            matches.append(impl_id)
        elif "seed" in normalized and ("seed" in name or "/seed" in path):
            matches.append(impl_id)
        elif ("start" in normalized or normalized in {"up", "server"} or "server" in normalized) and name in {
            "main.ts",
            "main.js",
            "server.ts",
            "server.js",
            "app.ts",
            "app.js",
            "index.ts",
            "index.js",
        }:
            matches.append(impl_id)
        elif "build" in normalized and name in {"package.json", "dockerfile"}:
            matches.append(impl_id)
    return matches


def _verification_target(path: Path) -> str:
    content = path.read_text(encoding="utf-8", errors="ignore")
    url_match = URL_PATH_RE.search(content)
    if url_match:
        return url_match.group(0).rstrip(".,)'\"")

    name = path.name
    for marker in (".test.", ".spec."):
        if marker in name:
            return name.split(marker, 1)[0]
    return path.stem


def _verification_template_ref(path: Path, *, cdp_browser_journey: bool = False) -> str:
    if cdp_browser_journey:
        return "cdp_browser"
    if path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return "playwright"
    if path.suffix == ".sh":
        return "curl"
    return "document"


def _has_cdp_browser_template(config: dict[str, Any]) -> bool:
    verification = config.get("verification")
    if not isinstance(verification, dict):
        return False
    templates = verification.get("templates")
    return isinstance(templates, dict) and "cdp_browser" in templates


def _iter_design_doc_user_journeys(design_docs: list | dict) -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(design_docs, dict):
        iterable = design_docs.items()
    else:
        iterable = enumerate(design_docs)

    for key, metadata in iterable:
        if not isinstance(metadata, dict):
            continue
        source_id = _design_doc_source_id(key, metadata)
        journeys = _design_doc_user_journey_entries(metadata)
        for journey in journeys:
            yield source_id, journey


def _design_doc_source_id(key: Any, metadata: dict[str, Any]) -> str:
    if isinstance(key, str):
        return _normalize_output_path(key)
    for field in ("id", "node_id", "path"):
        value = metadata.get(field)
        if isinstance(value, Path):
            return _normalize_output_path(value.as_posix())
        if isinstance(value, str) and value:
            return _normalize_output_path(value)
    return f"design_doc:{key}"


def _design_doc_user_journey_entries(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = metadata.get("attributes")
    if isinstance(attributes, dict):
        journeys = attributes.get("user_journeys")
        if isinstance(journeys, list):
            return [journey for journey in journeys if isinstance(journey, dict)]

    frontmatter = metadata.get("frontmatter")
    if isinstance(frontmatter, dict):
        journeys = frontmatter.get("user_journeys")
        if isinstance(journeys, list):
            return [journey for journey in journeys if isinstance(journey, dict)]

    return []


def _journey_name(journey: dict[str, Any]) -> str | None:
    value = journey.get("name")
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name or None


def _verification_target_for_journey(journey: dict[str, Any], fallback: str) -> str:
    for step in _as_list(journey.get("steps")):
        if not isinstance(step, dict):
            continue
        target = step.get("target") or step.get("url") or step.get("path")
        if isinstance(target, str) and target.strip():
            return target.strip()
    return fallback


def _unique_verification_identifier(
    tests: dict[str, VerificationTestNode],
    base_identifier: str,
    source_id: str,
) -> str:
    if base_identifier not in tests:
        return base_identifier

    source_slug = _slug(source_id)
    candidate = f"{base_identifier}:{source_slug}"
    if candidate not in tests:
        return candidate

    index = 2
    while f"{candidate}:{index}" in tests:
        index += 1
    return f"{candidate}:{index}"


def _load_design_doc_records(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = _design_doc_patterns(config)
    records: list[dict[str, Any]] = []
    for path in _glob_paths(project_root, patterns):
        if not path.is_file():
            continue
        frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        records.append(
            {
                "id": _relative_id(path, project_root),
                "path": path,
                "frontmatter": frontmatter,
                "body": body,
                "acceptance_criteria": frontmatter.get("acceptance_criteria"),
            }
        )
    return records


def _design_doc_patterns(config: dict[str, Any]) -> list[str]:
    direct = config.get("design_doc_patterns")
    if direct:
        return [str(item) for item in _as_list(direct)]

    dag = config.get("dag", {})
    if isinstance(dag, dict):
        node_extraction = dag.get("node_extraction", {})
        if isinstance(node_extraction, dict) and node_extraction.get("design_glob"):
            return [str(item) for item in _as_list(node_extraction["design_glob"])]
        if dag.get("design_doc_patterns"):
            return [str(item) for item in _as_list(dag["design_doc_patterns"])]

    scan = config.get("scan", {})
    if isinstance(scan, dict) and scan.get("doc_dirs"):
        patterns: list[str] = []
        for doc_dir in _as_list(scan["doc_dirs"]):
            base = str(doc_dir).strip().strip("/") or "docs"
            patterns.append(f"{base}/design/*.md")
        return patterns
    return ["docs/design/*.md"]


def _iter_design_docs(design_docs: list | dict) -> Iterable[dict[str, str]]:
    if isinstance(design_docs, dict):
        iterable = [{"id": key, **value} if isinstance(value, dict) else {"id": key, "value": value} for key, value in design_docs.items()]
    else:
        iterable = list(design_docs)

    for index, design_doc in enumerate(iterable):
        doc_id = f"design_doc:{index + 1}"
        criteria = ""
        if isinstance(design_doc, dict):
            doc_id = str(design_doc.get("id") or design_doc.get("node_id") or design_doc.get("path") or doc_id)
            frontmatter = design_doc.get("frontmatter")
            if isinstance(frontmatter, dict):
                criteria = _criteria_text(frontmatter.get("acceptance_criteria") or frontmatter.get("criteria"))
            criteria = criteria or _criteria_text(
                design_doc.get("acceptance_criteria") or design_doc.get("criteria") or design_doc.get("body") or design_doc
            )
        else:
            attributes = getattr(design_doc, "attributes", {})
            if isinstance(attributes, dict):
                frontmatter = attributes.get("frontmatter", {})
                if isinstance(frontmatter, dict):
                    criteria = _criteria_text(frontmatter.get("acceptance_criteria") or frontmatter.get("criteria"))
            doc_id = str(getattr(design_doc, "id", doc_id))
            criteria = criteria or _criteria_text(design_doc)
        yield {"id": _normalize_output_path(doc_id), "criteria": criteria}


def _criteria_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_criteria_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_criteria_text(item)}" for key, item in value.items())
    return str(value)


def _deployment_keywords(text: str) -> set[str]:
    normalized = text.lower()
    keywords: set[str] = set()
    keyword_groups = {
        "migrate": ("migrate", "migration"),
        "seed": ("seed",),
        "build": ("build",),
        "start": ("start", "server", "up"),
    }
    for keyword, needles in keyword_groups.items():
        if any(needle in normalized for needle in needles):
            keywords.add(keyword)
    return keywords


def _mentions_any(text: str, needles: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(needle in normalized for needle in needles)


def _iter_impl_files(impl_files: Any) -> Iterable[Any]:
    if isinstance(impl_files, dict):
        return impl_files.keys()
    return impl_files


def _impl_file_id(impl_file: Any) -> str:
    if isinstance(impl_file, str):
        return _normalize_output_path(impl_file)
    if isinstance(impl_file, Path):
        return _normalize_output_path(impl_file.as_posix())
    node_id = getattr(impl_file, "id", None)
    if isinstance(node_id, str):
        return _normalize_output_path(node_id)
    return _normalize_output_path(str(impl_file))


def _glob_paths(project_root: Path, patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(project_root.glob(pattern))
    return _dedupe_paths(paths)


def _existing_files(paths: Iterable[Path]) -> list[Path]:
    return [path for path in _dedupe_paths(paths) if path.is_file()]


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    deduped: dict[str, Path] = {}
    for path in paths:
        resolved = Path(path).resolve()
        deduped[str(resolved)] = resolved
    return [deduped[key] for key in sorted(deduped)]


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_edges(
    edges: Iterable[tuple[str, str, str, dict[str, Any]]],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    result: list[tuple[str, str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for from_id, to_id, kind, attributes in edges:
        key = (from_id, to_id, kind, repr(sorted(attributes.items())))
        if key in seen:
            continue
        seen.add(key)
        result.append((from_id, to_id, kind, attributes))
    return result


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _load_project_config_or_empty(project_root: Path) -> dict[str, Any]:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _project_path(project_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


def _relative_id(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _normalize_output_path(path_text: str) -> str:
    return Path(path_text).as_posix().lstrip("./")


def _section_key(value: str) -> str:
    return _slug(value.strip().split("{", 1)[0])


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "unknown"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if str(item).strip()]
