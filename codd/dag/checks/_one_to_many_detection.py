"""Detect one-to-many relation hints from DAG metadata."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


ONE_TO_MANY_RE = re.compile(
    r"\b(?:1\s*:\s*N|N\s*:\s*1|one[-\s]?to[-\s]?many|many[-\s]?to[-\s]?one)\b",
    re.IGNORECASE,
)
ARROW_PAIR_RE = re.compile(
    r"(?P<parent>[A-Za-z][A-Za-z0-9_-]*)\s*(?:->|=>|to|has|contains|owns|→)\s*"
    r"(?:many|multiple|複数の?\s*)?(?P<child>[A-Za-z][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
PAREN_PAIR_RE = re.compile(
    r"1\s*:\s*N\s*\(\s*(?P<parent>[A-Za-z][A-Za-z0-9_-]*)\s*(?:->|=>|to|→)\s*"
    r"(?:many\s+)?(?P<child>[A-Za-z][A-Za-z0-9_-]*)\s*\)",
    re.IGNORECASE,
)
MANY_TO_ONE_WITH_RE = re.compile(
    r"many[-\s]?to[-\s]?one\s+(?:with|to|against|relationship\s+with)\s+"
    r"(?P<parent>[A-Za-z][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)


def detect_one_to_many_relations(
    dag: Any | None = None,
    project_root: str | Path | None = None,
) -> list[dict[str, str]]:
    """Return detected ``parent``/``child`` relation records.

    The detector is intentionally schema-light: it reads lexicon descriptions,
    db_table relation metadata, and data_dependencies frontmatter without
    requiring a project-specific ORM or UI stack.
    """

    relations: list[dict[str, str]] = []
    if dag is not None:
        nodes = sorted(getattr(dag, "nodes", {}).values(), key=lambda item: getattr(item, "id", ""))
        relations.extend(_relations_from_dag_nodes(nodes))

    if project_root is not None:
        relations.extend(_relations_from_project_lexicon(Path(project_root)))

    return _dedupe_relations(relations)


def _relations_from_dag_nodes(nodes: Iterable[Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for node in nodes:
        attributes = _node_attributes(node)
        node_id = str(getattr(node, "id", ""))
        node_kind = str(getattr(node, "kind", ""))
        if node_id.startswith("lexicon:") or node_kind == "lexicon":
            relations.extend(_relations_from_lexicon_entry(_term_from_node(node, attributes), attributes))
        if node_id.startswith("db_table:") or node_kind == "db_table":
            relations.extend(_relations_from_db_table(node_id, attributes))
        relations.extend(_relations_from_data_dependencies(node_id, attributes))
    return relations


def _relations_from_project_lexicon(project_root: Path) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for path in _unique_paths([project_root / "project_lexicon.yaml", project_root / "lexicon.yaml"]):
        if not path.is_file():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for term, entry in _lexicon_entries(payload):
            relations.extend(_relations_from_lexicon_entry(term, entry))
    return relations


def _relations_from_lexicon_entry(term: str, entry: Mapping[str, Any]) -> list[dict[str, str]]:
    text = _flatten_text(entry)
    if not ONE_TO_MANY_RE.search(text):
        return []
    parent, child = _pair_from_text(text, fallback_child=term)
    if not parent or not child or _same_term(parent, child):
        return []
    return [
        {
            "parent": parent,
            "child": child,
            "evidence": f"lexicon {term} declares one-to-many relation",
        }
    ]


def _relations_from_db_table(node_id: str, attributes: Mapping[str, Any]) -> list[dict[str, str]]:
    table = _clean_term(node_id.removeprefix("db_table:"))
    relations: list[dict[str, str]] = []
    for relation in _relation_entries(attributes):
        text = _flatten_text(relation)
        if not ONE_TO_MANY_RE.search(text):
            continue
        parent, child = _pair_from_relation(table, relation, text)
        if not parent or not child or _same_term(parent, child):
            continue
        relations.append(
            {
                "parent": parent,
                "child": child,
                "evidence": f"db_table {table} relation metadata declares one-to-many relation",
            }
        )
    return relations


def _relations_from_data_dependencies(node_id: str, attributes: Mapping[str, Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for dependency in _data_dependency_entries(attributes):
        text = _flatten_text(dependency)
        if not ONE_TO_MANY_RE.search(text):
            continue
        parent = _clean_term(
            dependency.get("parent")
            or dependency.get("one")
            or dependency.get("source")
            or dependency.get("table")
            or ""
        )
        child = _clean_term(
            dependency.get("child")
            or dependency.get("many")
            or dependency.get("target")
            or dependency.get("entity")
            or _child_from_affects(dependency.get("affects"))
        )
        if not parent or not child:
            parent, child = _pair_from_text(text, fallback_child=child)
        if not parent or not child or _same_term(parent, child):
            continue
        relations.append(
            {
                "parent": parent,
                "child": child,
                "evidence": f"{node_id} data_dependencies declare one-to-many relation",
            }
        )
    return relations


def _pair_from_relation(table: str, relation: Mapping[str, Any], text: str) -> tuple[str, str]:
    target = _clean_term(
        relation.get("child")
        or relation.get("target")
        or relation.get("to")
        or relation.get("table")
        or relation.get("model")
        or ""
    )
    parent = _clean_term(relation.get("parent") or relation.get("from") or "")
    if re.search(r"\b(?:many[-\s]?to[-\s]?one|N\s*:\s*1)\b", text, re.IGNORECASE) and target:
        return target, table
    if parent and target:
        return parent, target
    if target:
        return table, target
    return _pair_from_text(text, fallback_child="")


def _pair_from_text(text: str, *, fallback_child: str) -> tuple[str, str]:
    match = PAREN_PAIR_RE.search(text) or ARROW_PAIR_RE.search(text)
    if match:
        return _clean_term(match.group("parent")), _clean_term(match.group("child"))
    match = MANY_TO_ONE_WITH_RE.search(text)
    if match and fallback_child:
        return _clean_term(match.group("parent")), _clean_term(fallback_child)
    return "", _clean_term(fallback_child)


def _relation_entries(attributes: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Any] = [attributes.get("relations")]
    frontmatter = attributes.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        values.append(frontmatter.get("relations"))
        codd_meta = frontmatter.get("codd")
        if isinstance(codd_meta, Mapping):
            values.append(codd_meta.get("relations"))
    details = attributes.get("details")
    if isinstance(details, Mapping):
        values.append(details.get("relations"))
    return [item for value in values for item in _mapping_items(value)]


def _data_dependency_entries(attributes: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Any] = [attributes.get("data_dependencies")]
    frontmatter = attributes.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        values.append(frontmatter.get("data_dependencies"))
        codd_meta = frontmatter.get("codd")
        if isinstance(codd_meta, Mapping):
            values.append(codd_meta.get("data_dependencies"))
    return [item for value in values for item in _mapping_items(value)]


def _lexicon_entries(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for key in ("terms", "entries", "glossary", "entities"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            for term, entry in value.items():
                if isinstance(entry, Mapping):
                    entries.append((_clean_term(term), entry))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    term = _clean_term(item.get("id") or item.get("term") or item.get("name") or "")
                    if term:
                        entries.append((term, item))
    return entries


def _node_attributes(node: Any) -> Mapping[str, Any]:
    attributes = getattr(node, "attributes", {}) or {}
    return attributes if isinstance(attributes, Mapping) else {}


def _term_from_node(node: Any, attributes: Mapping[str, Any]) -> str:
    for key in ("term", "name", "id"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_term(value)
    return _clean_term(str(getattr(node, "id", "")).removeprefix("lexicon:"))


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _child_from_affects(value: Any) -> str:
    if isinstance(value, list) and value:
        return _clean_term(str(value[0]).split(":", 1)[-1].split(".")[0])
    return ""


def _flatten_text(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [str(key) for key in value]
        parts.extend(_flatten_text(item) for item in value.values())
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _dedupe_relations(relations: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for relation in relations:
        parent = _clean_term(relation.get("parent"))
        child = _clean_term(relation.get("child"))
        if not parent or not child:
            continue
        key = (_norm(parent), _norm(child))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "parent": parent,
                "child": child,
                "evidence": str(relation.get("evidence") or "one-to-many relation detected"),
            }
        )
    return deduped


def _clean_term(value: Any) -> str:
    text = str(value or "").strip().strip("`'\"")
    return re.sub(r"\s+", "_", text)


def _same_term(left: str, right: str) -> bool:
    return _norm(left) == _norm(right)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


__all__ = ["detect_one_to_many_relations"]
