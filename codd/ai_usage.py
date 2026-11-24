"""Opt-in AI invocation metadata and implementation routing events.

No prompts, responses, argv, environments or exception messages are persisted.
File-writing output length measures the returned file contract; other routes
measure stdout (including failed commands). Direct file-writing-helper calls and
unrelated direct subprocess sites are outside this recorder's scope.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, Mapping


@dataclass
class _Session:
    path: Path
    stage: str
    attempt: int = 0


@dataclass
class _Trial:
    route: str
    output_chars: int = 0
    outcome: str = "success"


_session: ContextVar[_Session | None] = ContextVar("ai_usage_session", default=None)
_trial: ContextVar[_Trial | None] = ContextVar("ai_usage_trial", default=None)


def resolve_usage_log(config: Mapping[str, Any] | None = None, project_root=None) -> Path | None:
    """Resolve env before config (an empty env value disables logging).

    Relative paths use the project root or current working directory.
    """
    if "CODD_AI_USAGE_LOG" in os.environ:
        raw = os.environ["CODD_AI_USAGE_LOG"]
    else:
        if config is None:
            from codd.config import load_project_config

            try:
                config = load_project_config(Path(project_root or Path.cwd()))
            except FileNotFoundError:
                config = None
        section = config.get("ai") if isinstance(config, Mapping) else None
        if section is not None and not isinstance(section, Mapping):
            raise ValueError("ai must contain a YAML mapping")
        raw = section.get("usage_log") if section is not None else None
    if raw is None:
        return None
    if not isinstance(raw, (str, Path)):
        raise ValueError("ai.usage_log must be a path string or null")
    if not str(raw).strip():
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (Path(project_root or Path.cwd()) / path).resolve()


@contextmanager
def usage_session(config=None, project_root=None, stage: str | None = None):
    """Share attempt indices across recovery and adapter fallback without duplication."""
    token = None
    if _session.get() is None:
        try:
            path = resolve_usage_log(config, project_root)
            if path is not None:
                if stage is None:
                    import click

                    ctx = click.get_current_context(silent=True)
                    names = []
                    while ctx is not None and ctx.parent is not None:
                        if ctx.info_name:
                            names.append(ctx.info_name)
                        ctx = ctx.parent
                    stage = " ".join(reversed(names))
                token = _session.set(_Session(path, stage or ""))
        except Exception:
            pass  # Recording must never change the AI call's result.
    try:
        yield
    finally:
        if token is not None:
            _session.reset(token)


def command_metadata(command, model: str | None = None) -> tuple[str, str]:
    """Allow only executable basename and the value of an explicit model option."""
    parts = shlex.split(command) if isinstance(command, str) else list(command or [])
    if model is None:
        for index, part in enumerate(parts):
            if part in {"--model", "-m"} and index + 1 < len(parts):
                model = parts[index + 1]
                break
            if part.startswith("--model="):
                model = part.split("=", 1)[1]
                break
    return (Path(parts[0]).name if parts else "", model or "")


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def note_implement_routing(
    *, route: str, reason: str, detail: str, fallback: bool, design_node: str, outputs: list[str],
    incomplete_distant_artifacts: int,
) -> None:
    """Record a task decision without advancing the AI call counter."""
    session = _session.get()
    if session is None:
        return
    try:
        _append(session.path, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": session.stage,
            "event": "implement_routing",
            "route": route,
            "reason": reason,
            "detail": detail,
            "fallback": fallback,
            "design_node": design_node,
            "outputs": outputs,
            "incomplete_distant_artifacts": incomplete_distant_artifacts,
        })
    except Exception:
        pass  # Recording must never change the AI call's result.


@contextmanager
def usage_trial(prompt: str, route: str, command, model: str | None = None, *, defer_if_nested=False):
    session = _session.get()
    if session is None:
        yield
        return
    try:
        started = time.monotonic()
    except Exception:
        yield
        return
    trial = _Trial(route)
    previous_attempt = session.attempt
    if not defer_if_nested:
        session.attempt += 1
    attempt = session.attempt
    token = _trial.set(trial)
    try:
        yield
    except BaseException as exc:
        if trial.outcome == "success":
            trial.outcome = type(exc).__name__
        raise
    finally:
        _trial.reset(token)
        try:
            if not defer_if_nested or session.attempt == previous_attempt:
                if defer_if_nested:
                    session.attempt += 1
                    attempt = session.attempt
                executable, resolved_model = command_metadata(command() if callable(command) else command, model)
                _append(session.path, {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "stage": session.stage, "route": trial.route,
                    "executable": executable, "model": resolved_model,
                    "prompt_chars": len(prompt),
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "output_chars": trial.output_chars,
                    "seconds": time.monotonic() - started,
                    "outcome": trial.outcome, "attempt": attempt,
                })
        except Exception:
            pass


def note_output(output: object) -> None:
    trial = _trial.get()
    if trial is not None and issubclass(type(output), str):
        # Measure strings without invoking a subclass's custom __len__.
        trial.output_chars = str.__len__(output)


def note_failure(kind: str) -> None:
    trial = _trial.get()
    if trial is not None:
        trial.outcome = kind


def note_route(route: str) -> None:
    trial = _trial.get()
    if trial is not None:
        trial.route = route


def git_usage_exclusion(project_root: Path) -> tuple[list[str], Path | None]:
    """Return the Git exclusion and the log's path within the project, if any."""
    session = _session.get()
    if session is None:
        return [], None
    try:
        if session.path.is_dir():
            return [], None  # An invalid log destination must not exclude a whole tree.
        root = project_root.resolve()
        if session.path.is_relative_to(root):
            relative = session.path.relative_to(root)
            return [f":(literal,exclude){relative.as_posix()}"], relative

        # Staging and diff also see logs in the repository outside project_root.
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=str(project_root),
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0 or not result.stdout:
            return [], None
        relative = session.path.relative_to(Path(result.stdout.removesuffix("\n")))
    except (OSError, RuntimeError, ValueError):
        return [], None
    return [f":(top,literal,exclude){relative.as_posix()}"], None


def summarize_usage(path: Path) -> tuple[list[dict], int]:
    """Aggregate trials by stage and count invalid JSONL lines."""
    groups: dict[str, dict] = {}
    hashes: dict[str, set[str]] = {}
    invalid_lines = 0
    with path.open("rb") as stream:
        for line in stream:
            try:
                record = json.loads(line.decode("utf-8"))
                if isinstance(record, dict) and record.get("event") == "implement_routing":
                    continue
                stage = record["stage"]
                prompt_hash = record["prompt_sha256"]
                prompt_chars = record["prompt_chars"]
                output_chars = record["output_chars"]
                seconds = record["seconds"]
                valid = (
                    isinstance(stage, str)
                    and isinstance(prompt_hash, str)
                    and type(prompt_chars) is int and prompt_chars >= 0
                    and type(output_chars) is int and output_chars >= 0
                    and type(seconds) in (float, int) and seconds >= 0 and math.isfinite(seconds)
                )
            except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
                valid = False
            if not valid:
                invalid_lines += 1
                continue
            group = groups.setdefault(stage, dict(stage=stage, calls=0, prompt_chars=0,
                                                  output_chars=0, seconds=0.0, reissues=0))
            group["calls"] += 1
            group["prompt_chars"] += prompt_chars
            group["output_chars"] += output_chars
            group["seconds"] += seconds
            seen = hashes.setdefault(stage, set())
            if prompt_hash in seen:
                group["reissues"] += 1
            seen.add(prompt_hash)
    return [groups[stage] for stage in sorted(groups)], invalid_lines
