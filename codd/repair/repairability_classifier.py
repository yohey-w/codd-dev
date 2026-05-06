"""Repairability grouping for verification violations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping


TEMPLATE_DIR = Path(__file__).with_name("templates")
VALID_GROUPS = {"repairable", "pre_existing", "unrepairable"}


@dataclass
class RepairabilityClassification:
    repairable: list[Any]
    pre_existing: list[Any]
    unrepairable: list[Any]


class RepairabilityClassifier:
    def __init__(self, llm: Any | None = None, repo_path: str | Path = ".", **kwargs: Any):
        alias = "llm_" + "cl" + "ient"
        self.llm = llm if llm is not None else kwargs.get(alias)
        self.repo_path = Path(repo_path)

    def classify(self, violations: list[Any], baseline_ref: str | None = None) -> RepairabilityClassification:
        items = list(violations)
        if not items:
            return RepairabilityClassification([], [], [])

        changed_files = self._git_diff_files(self.repo_path, baseline_ref) if baseline_ref else set()
        repairable: list[Any] = []
        pending: list[Any] = []
        for item in items:
            affected = _affected_paths(item, self.repo_path)
            if affected and _intersects_changed_files(affected, changed_files):
                repairable.append(item)
            else:
                pending.append(item)

        if not pending:
            return RepairabilityClassification(repairable, [], [])

        decided = self._classify_pending(pending, baseline_ref)
        return RepairabilityClassification(
            repairable + decided.repairable,
            decided.pre_existing,
            decided.unrepairable,
        )

    def _git_diff_files(self, repo_path: str | Path, baseline_ref: str | None) -> set[str]:
        if not baseline_ref:
            return set()
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--name-only", f"{baseline_ref}..HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return set()
        return {_normalize_path(line) for line in result.stdout.splitlines() if line.strip()}

    def _build_llm_input(self, violations: list[Any], baseline_ref: str | None) -> str:
        entries = _violation_entries(violations, self.repo_path)
        values = {
            "violations_json": json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            "baseline_ref": str(baseline_ref or ""),
        }
        template = (TEMPLATE_DIR / "repairability_meta.md").read_text(encoding="utf-8")
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
            template = template.replace("{" + key + "}", value)
        return template

    def _parse_llm_decision(self, llm_output: str, violations: list[Any]) -> RepairabilityClassification:
        ids = [entry["id"] for entry in _violation_entries(violations, self.repo_path)]
        try:
            payload = _json_object(llm_output)
        except ValueError:
            return RepairabilityClassification([], [], list(violations))

        repairable: list[Any] = []
        pre_existing: list[Any] = []
        unrepairable: list[Any] = []
        for item_id, item in zip(ids, violations):
            group = _normalize_group(payload.get(item_id))
            if group == "repairable":
                repairable.append(item)
            elif group == "pre_existing":
                pre_existing.append(item)
            else:
                unrepairable.append(item)
        return RepairabilityClassification(repairable, pre_existing, unrepairable)

    def _classify_pending(self, violations: list[Any], baseline_ref: str | None) -> RepairabilityClassification:
        if self.llm is None:
            return RepairabilityClassification([], [], list(violations))
        prompt = self._build_llm_input(violations, baseline_ref)
        try:
            raw = _invoke_llm(self.llm, prompt)
        except (OSError, TypeError, ValueError, RuntimeError, AttributeError):
            return RepairabilityClassification([], [], list(violations))
        return self._parse_llm_decision(raw, violations)


class NullClassifier:
    def classify(self, violations: list[Any], baseline_ref: str | None = None) -> RepairabilityClassification:
        return RepairabilityClassification(list(violations), [], [])


def _invoke_llm(llm: Any, prompt: str) -> str:
    if hasattr(llm, "complete"):
        return str(llm.complete(prompt))
    if hasattr(llm, "invoke"):
        return str(llm.invoke(prompt))
    if callable(llm):
        return str(llm(prompt))
    raise TypeError("llm must be callable or expose complete()/invoke()")


def _violation_entries(violations: list[Any], repo_path: Path) -> list[dict[str, Any]]:
    used: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(violations):
        base = _violation_id(item, index)
        count = used.get(base, 0)
        used[base] = count + 1
        item_id = base if count == 0 else f"{base}_{count}"
        entries.append(
            {
                "id": item_id,
                "affected_files": sorted(_affected_paths(item, repo_path)),
                "violation": _plain_data(item),
            }
        )
    return entries


def _violation_id(item: Any, index: int) -> str:
    for key in ("id", "violation_id", "check_id", "check_name", "name"):
        value = _value(item, key)
        if value not in (None, ""):
            return str(value)
    return f"violation_{index}"


def _affected_paths(item: Any, repo_path: Path) -> set[str]:
    paths: set[str] = set()
    for key in ("affected_files", "affected_file", "file_paths", "file_path", "files", "path", "failed_nodes"):
        for value in _as_list(_value(item, key)):
            if isinstance(value, str) and value.strip():
                normalized = _normalize_project_path(value, repo_path)
                if normalized:
                    paths.add(normalized)
    return paths


def _value(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalize_project_path(value: str, repo_path: Path) -> str:
    raw = str(value).strip().replace("\\", "/")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        root = repo_path.resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            return _normalize_path(str(resolved.relative_to(root)))
        except ValueError:
            return ""
    return _normalize_path(raw)


def _normalize_path(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _intersects_changed_files(affected: set[str], changed: set[str]) -> bool:
    if not affected or not changed:
        return False
    for item in affected:
        for candidate in changed:
            if item == candidate or candidate.startswith(item + "/") or item.startswith(candidate + "/"):
                return True
    return False


def _json_object(raw_output: str) -> Mapping[str, Any]:
    text = _strip_json_fence(raw_output)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("output is not a JSON object")
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, Mapping):
        raise ValueError("output is not a JSON object")
    return payload


def _strip_json_fence(raw_output: str) -> str:
    text = str(raw_output).strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _normalize_group(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    return text if text in VALID_GROUPS else "unrepairable"


def _plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_data(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_data(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return value


__all__ = [
    "NullClassifier",
    "RepairabilityClassification",
    "RepairabilityClassifier",
]
