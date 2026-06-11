"""Advisory config-key validation (typo guard) for project ``codd.yaml`` files.

CoDD's config loader (:mod:`codd.config`) deep-merges the project ``codd.yaml``
over ``codd/defaults.yaml`` and silently keeps unknown keys: a typo'd key
(``enabld:``, ``surface_reconcilation:``) is never read by any consumer, so
the feature it was meant to configure silently stays at its default. This
module derives the KNOWN key tree from ``defaults.yaml`` (recursively), adds
every top-level key the code reads but the defaults file does not declare,
and reports unknown keys with did-you-mean suggestions.

The check is ADVISORY ONLY: it feeds ``codd doctor`` warnings (and therefore
the doctor section of ``codd check``) and never affects exit codes.

Two kinds of deliberate looseness keep the false-positive rate at zero:

1. OPEN sections — mappings whose CHILD keys are user-defined are never
   validated below their root (see ``_OPEN_SECTIONS`` for the per-entry
   rationale).
2. List values are never validated — list items (runtime targets,
   conventions, policies, filesystem route configs, ...) carry their own
   item-level schemas owned by their consumers, not by ``defaults.yaml``.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from codd.config import DEFAULTS_PATH, find_codd_dir

__all__ = [
    "validate_config_keys",
    "project_config_key_warnings",
]


class _Open:
    """Sentinel: anything below this key is user-defined — do not validate."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<open>"


_OPEN = _Open()

#: Sections whose child keys are user-defined. Validation stops at the root.
#: Every entry must carry a justification grounded in how the code reads it.
_OPEN_SECTIONS: tuple[tuple[str, ...], ...] = (
    # Named per-command AI overrides: codd/ai_invoke.py looks up arbitrary
    # command names (plan_derive, impl_step_derive, criteria_expand, ...)
    # under ai_commands; projects add new names freely.
    ("ai_commands",),
    # User-defined capability name -> pattern list mapping (defaults: {}).
    ("coherence", "capability_patterns"),
    # User-defined frontmatter alias -> canonical key mapping (defaults: {}).
    ("extraction", "frontmatter_alias"),
    # Entire user-authored operations section: operation ids/structure are
    # project-specific (cli.py _operation_flows_from_project).
    ("operation_flow",),
    # Policy entries are author-defined documents (codd/policy.py reads the
    # raw structure as data).
    ("policies",),
    # Project metadata: codd reads project.frameworks, but the section also
    # carries free-form metadata (name, type, ...) that many consumers read
    # opportunistically (config.get("project") sites across the codebase).
    ("project",),
    # DAG settings are read dynamically: dag/builder.py _normalize_dag_section
    # accepts suffix/pattern keys, dag/runner.py reads enabled_checks, and
    # individual registered checks read their own per-check config blocks.
    ("dag",),
    # Stage names are user-defined; each maps to required artifact ids
    # (defaults: {}).
    ("artifact_contract", "stages"),
)

#: Top-level keys the CODE reads from the project config but defaults.yaml
#: does not declare. Children are not validated (their sub-schemas live in
#: the consuming modules, not in defaults.yaml). Each entry cites a reader.
_CODE_READ_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "ci",                     # deployer.py (CiConfig.from_mapping(codd_config.get("ci")))
    "codex_app_server",       # deployment/providers/ai_command_factory.py:_section
    "common_node_patterns",   # dag/builder.py:_apply_common_node_patterns (top-level form)
    "coverage",               # cli.py check payload (full_config.get("coverage"))
    "deployment",             # deployment/extractor.py (config.get("deployment"))
    "design_doc_extraction",  # dag/builder.py direct_keys
    "design_doc_patterns",    # dag/builder.py direct_keys
    "design_md",              # cli.py:_load_coherence_context
    "design_md_path",         # cli.py:_load_coherence_context
    "document_url_linking",   # drift.py (config.get("document_url_linking"))
    "e2e",                    # drift.py / e2e_generator.py
    "filesystem_routes",      # drift.py / screen_transition_extractor.py / scanner.py
    "impl_file_patterns",     # dag/builder.py direct_keys
    "implement",              # implementer.py / cli.py (config.get("implement"))
    "implementation_suffixes",         # dag/builder.py direct_keys
    "implementation_suffixes_extend",  # dag/builder.py direct_keys
    "implementer",            # implementer.py (config.get("implementer"))
    "import_aliases",         # dag/builder.py direct_keys
    "lexicon_file",           # dag/builder.py direct_keys
    "lexicon_path",           # cli.py:_load_coherence_context
    "llm",                    # dag/builder.py (project_config.get("llm"))
    "notification",           # llm/approval.py (notification.ntfy_command / thresholds)
    "operation_flow",         # cli.py:_operation_flows_from_project
    "opt_outs",               # dag/checks/opt_out.py
    "plan_task_file",         # dag/builder.py direct_keys
    "policies",               # policy.py (config.get("policies"))
    "preflight",              # preflight/__init__.py (codd_yaml.get("preflight"))
    "repair",                 # cli.py repair commands (config.get("repair"))
    "required_artifacts",     # generator.py / required_artifacts_deriver.py / artifact_ids.py
    "requirement_docs",       # required_artifacts_deriver.py / cli.py
    "runtime_smoke",          # runtime_smoke/config.py / cli.py doctor helpers
    "screen_flow",            # cli.py / coverage_metrics.py / deployer.py
    "screen_flow_drift",      # drift.py
    "screen_transitions",     # screen_transition_extractor.py
    "service_boundaries",     # validator.py / require.py
    "test",                   # watch/test_runner.py (config.get("test"))
    "test_file_patterns",     # dag/builder.py direct_keys
    "test_runners",           # watch/test_runner.py
    "test_suffixes",          # dag/builder.py direct_keys
    "test_suffixes_extend",   # dag/builder.py direct_keys
    "ui_coherence",           # dag/checks/ui_coherence.py
    "verification",           # cli.py / deployment/extractor.py
    "wave_config",            # planner.py / validator.py
)

#: Nested keys the CODE reads inside sections that defaults.yaml DOES declare
#: (so the section is closed) but whose defaults entry omits them.
_CODE_READ_NESTED_KEYS: tuple[tuple[str, ...], ...] = (
    ("scan", "common_node_patterns"),          # dag/builder.py (nested form)
    ("coherence", "capability_requirements"),  # capability completeness reader
    ("coherence", "runtime_capability_inference"),
    ("coherence", "design_md"),                # cli.py:_load_coherence_context
    ("coherence", "design_md_path"),
    ("coherence", "lexicon"),
    ("coherence", "lexicon_path"),
    ("runtime", "global_action_targets"),      # cli.py:_has_global_action_targets
    ("runtime", "role_sequence_targets"),      # cli.py:_runtime_outcome_entries
    ("requirement_completeness", "hitl_mode"),  # requirement_completeness_auditor.py
    ("verify", "verification_timeout"),        # repair/verify_runner.py
)


def _load_defaults() -> dict[str, Any]:
    import yaml

    payload = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _tree_from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Recursive known-key tree: dict values become subtrees, leaves become {}."""
    tree: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, dict):
            tree[str(key)] = _tree_from_mapping(value)
        else:
            tree[str(key)] = {}
    return tree


def _insert(tree: dict[str, Any], path: tuple[str, ...], node: Any) -> None:
    cursor = tree
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    existing = cursor.get(path[-1])
    if node is _OPEN or not isinstance(existing, dict) or not existing:
        cursor[path[-1]] = node


def _known_tree(defaults: dict[str, Any] | None) -> dict[str, Any]:
    base = defaults if defaults is not None else _load_defaults()
    tree = _tree_from_mapping(base)
    for key in _CODE_READ_TOP_LEVEL_KEYS:
        _insert(tree, (key,), _OPEN)
    for path in _CODE_READ_NESTED_KEYS:
        _insert(tree, path, _OPEN)
    for path in _OPEN_SECTIONS:
        _insert(tree, path, _OPEN)
    return tree


def _walk(
    mapping: dict[str, Any],
    known: dict[str, Any],
    path: tuple[str, ...],
    warnings: list[str],
) -> None:
    for raw_key, value in mapping.items():
        key = str(raw_key)
        node = known.get(key)
        if key in known:
            if isinstance(node, dict) and node and isinstance(value, dict):
                _walk(value, node, (*path, key), warnings)
            continue
        dotted = ".".join((*path, key))
        suggestion = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
        if suggestion:
            warnings.append(
                f"unknown config key '{dotted}' — did you mean '{suggestion[0]}'? "
                "(unknown keys are silently ignored)"
            )
        else:
            warnings.append(
                f"unknown config key '{dotted}' (unknown keys are silently ignored)"
            )


def validate_config_keys(
    project_config: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
) -> list[str]:
    """Return advisory warnings for unknown keys in *project_config*.

    *project_config* should be the RAW project ``codd.yaml`` mapping (not the
    defaults-merged config), so warnings only ever point at keys the user
    actually wrote. *defaults* overrides the shipped ``defaults.yaml`` —
    intended for tests.
    """
    if not isinstance(project_config, dict):
        return []
    warnings: list[str] = []
    _walk(project_config, _known_tree(defaults), (), warnings)
    return warnings


def project_config_key_warnings(project_root: Path) -> list[str]:
    """Validate the raw ``codd.yaml`` of *project_root* (doctor entry point).

    Never raises: a missing or malformed file is some other check's problem
    (``load_project_config`` already fails doctor loudly in that case).
    """
    import yaml

    try:
        codd_dir = find_codd_dir(project_root)
        if codd_dir is None:
            return []
        payload = yaml.safe_load((codd_dir / "codd.yaml").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    return validate_config_keys(payload)
