"""One speculative repair of explicitly owned files before ordinary stage rework."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Iterable, Sequence

from codd.ai_usage import active_usage_log, recorded_usage_session, resolve_usage_log, summarize_usage
from codd.greenfield.pipeline import (
    ImplementTaskRef, OwnerFirstRestoreError, _declared_output_is_file_path, _is_glob_decl, _output_paths_for_task,
    _task_declares_no_authored_artifact,
)
from codd.implementer import (
    _root_artifact_destination, _root_artifact_overwrite_blocked, _root_artifact_patterns_from_config,
)
from codd.requirement_appendix import protect_requirement_appendix


# These files are written by the ordinary implementer. They are reproduced
# for both runs; only their work-copy changes are ignored, never published.
_RECORDS = {Path(".codd/dag.json"), Path(".codd/audit/reference_resolution.jsonl")}
_GIT_RECORDS = {Path(".git/index"), Path(".git/ORIG_HEAD"), Path(".git/logs/HEAD")}
# A byte copy must not accidentally duplicate an installed environment/cache.
# Count the complete tree (including Git) before allocating either snapshot.
_COPY_BYTE_LIMIT = 16 * 1024 * 1024
_State = dict[Path, tuple[int, bytes | str | None]]


class _Declined(Exception):
    pass


def owner_first_retry_enabled(config: dict) -> bool:
    section = config.get("implement")
    return isinstance(section, dict) and section.get("owner_first_retry_on_copy") is True


def _portable(value: Any, *, command: bool = False) -> bool:
    """Refuse location overrides we cannot carry unchanged to a different root."""
    if isinstance(value, dict):
        return all(_portable(key) and _portable(item, command=command or "command" in str(key))
                   for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return all(_portable(item, command=command) for item in value)
    if isinstance(value, (str, Path)):
        tokens = re.split(r"""[\s"'=:,;()]+""", str(value)) if command else [str(value).strip()]
        for token in tokens:
            if token.startswith(("/", "~", "\\")) or ".." in Path(token).parts:
                return False
    return True


def _owners(root, tasks, targets, config, eligible, skip_all):
    """Stage-approved inputs only. Directory and glob claims never elect an owner."""
    from codd.greenfield.test_rederivation import _declared_evidence

    selected = set()
    eligible_ids = {task.task_id for task in eligible}
    if len({task.task_id for task in tasks}) != len(tasks):
        return []
    for rel in targets:
        path = root / rel
        if (path.resolve(strict=True) != path or not path.is_file() or path.stat().st_nlink != 1
                or not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK | os.X_OK)):
            return []
        matches = []
        for task in tasks:
            if _task_declares_no_authored_artifact(task, config):
                continue
            files = [Path(raw) for raw in _declared_evidence(task)
                     if not _is_glob_decl(str(raw)) and not str(raw).endswith(("/", "/."))]
            if any(item != rel and (root / item).resolve() == path for item in files):
                return []
            if rel in files:
                matches.append(task)
        if len(matches) != 1:
            return []
        task = matches[0]
        if task.source not in {"configured", "derived"} or task.task_id not in eligible_ids:
            return []
        outputs = task.output_paths or _output_paths_for_task(config, task)
        protect_requirement_appendix(root, [Path(raw) for raw in (*outputs, *task.expected_outputs)])
        prefixes = [PurePosixPath(str(out).strip()) for out in outputs]
        for prefix in prefixes:
            dest = root / prefix
            if (prefix.is_absolute() or ".." in prefix.parts or
                    any(parent.exists() and not parent.is_dir() for parent in dest.parents) or
                    (dest.exists() and dest.is_file() != _declared_output_is_file_path(str(prefix)))):
                return []
        relative = PurePosixPath(rel.as_posix())
        routed = _root_artifact_destination(relative, prefixes, _root_artifact_patterns_from_config(config))
        if routed is not None and routed != relative:
            return []
        if not any(relative.is_relative_to(prefix) for prefix in prefixes):
            if routed != relative or _root_artifact_overwrite_blocked(path, rel.as_posix()):
                return []
        selected.add(task.task_id)
    if skip_all and selected == eligible_ids:
        return []
    return [task for task in tasks if task.task_id in selected]


def _inventory(root: Path, *, original: Path | None = None) -> _State:
    """lstat, link text, mode and exact file contents, including ignored/hidden entries."""
    result = {Path("."): (root.lstat().st_mode, None)}
    size = 0
    pending = [root]
    while pending:
        for path in pending.pop().iterdir():
            rel = path.relative_to(root)
            metadata = path.lstat()
            mode = metadata.st_mode
            value = None
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISLNK(mode):
                value = os.readlink(path)
                if original is not None and (os.path.isabs(value) or not path.resolve().is_relative_to(root)):
                    raise _Declined("P2: link leaves the project copy")
            elif stat.S_ISREG(mode):
                size += metadata.st_size
                if size > _COPY_BYTE_LIMIT:
                    raise _Declined("P2: full project exceeds the disposable copy limit")
                if metadata.st_nlink > 1:
                    raise _Declined("P2: shared file inode")
                data = path.read_bytes()
                if original is not None and os.fsencode(original) in data:
                    raise _Declined("P2: an input embeds the live project path")
                if original is not None and b"/" in data and b"\0" not in data:
                    _rebase_text(os.fsdecode(data), original, None)
                value = hashlib.sha256(data).digest()
            else:
                raise _Declined("P2/P4: special filesystem entry")
            result[rel] = mode, value
    return result


def _check_git(root: Path, state: _State) -> None:
    git = root / ".git"
    if not git.exists():
        if any((parent / ".git").exists() for parent in root.parents):
            raise _Declined("P2: enclosing repository")
    elif not git.is_dir() or git.is_symlink():
        raise _Declined("P2: linked Git directory")
    if any(name.name == ".git" and len(name.parts) > 1 for name in state):
        raise _Declined("P2: nested repository")
    for name in ("commondir", "gitdir", "worktrees", "objects/info/alternates", "objects/info/http-alternates"):
        if (git / name).exists():
            raise _Declined("P2: shared Git metadata")
    if any(key in os.environ for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG", "GIT_QUARANTINE_PATH",
    )):
        raise _Declined("P2: redirected Git environment")
    if any(os.environ.get(key, os.devnull) != os.devnull for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")):
        raise _Declined("P2: redirected Git configuration file")
    if git.is_dir():
        # Include system, global, local and conditional configuration. Do not
        # execute hooks, filters or fsmonitor programs while examining a copy.
        config = subprocess.run(
            ["git", "config", "--includes", "--name-only", "--get-regexp",
             r"^(core\.(worktree|bare|hookspath|fsmonitor)|include(if)?\.|filter\.)"],
            cwd=root, capture_output=True, check=False,
        )
        keys = set(config.stdout.splitlines())
        if config.returncode not in {0, 1} or keys - {b"core.bare"}:
            raise _Declined("P2: Git configuration redirects the worktree or invokes external programs")
        toplevel = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root)
        if Path(os.fsdecode(toplevel.rstrip(b"\r\n"))).resolve() != root:
            raise _Declined("P2: Git worktree is not the project root")


_PATH_TOKEN = re.compile(
    r"""(?P<quote>["'])(?P<quoted>~?/[^"'\n]*?)(?P=quote)|(?P<bare>~?/[^\s"'=:,;(){}\[\]<>|`!?\#]+)"""
)


def _copy_path(value: str, root: Path, copy: Path | None) -> str:
    location = Path(value).expanduser().resolve()
    if location != root.parent and not location.is_relative_to(root):
        return value
    if copy is None:
        raise _Declined("P2: input refers to the live project or its parent")
    if location == root.parent:
        return str(copy.parent)
    return str(copy / location.relative_to(root))


def _rebase_text(value: str, root: Path, copy: Path | None) -> str:
    """Translate project paths, including aliases in prose and command arguments."""
    def replace(match: re.Match) -> str:
        quote = match["quote"] or ""
        token = match["quoted"] or match["bare"]
        moved = _copy_path(token, root, copy)
        if not quote and moved == token and token.endswith("."):
            moved = _copy_path(token[:-1], root, copy) + "."
        return quote + moved + quote

    result = _PATH_TOKEN.sub(replace, value)
    # Unquoted paths containing whitespace can be ambiguous in free-form text.
    # Refuse any remaining literal reference, leaving neighbouring paths alone.
    end = r"""(?=$|[^\w./-]|\.(?:\s|$))"""
    if (re.search(re.escape(str(root)) + "(?:/|" + end + ")", result)
            or re.search(re.escape(str(root.parent)) + "/?" + end, result)):
        raise _Declined("P2: ambiguous live path in a copy input")
    return result


def _copy_env_value(value: str, root: Path, copy: Path) -> str:
    parts = value.split(os.pathsep)
    for index, part in enumerate(parts):
        if os.path.isabs(part) or part.startswith("~/"):
            moved = _copy_path(part, root, copy)
            if moved != part:
                parts[index] = moved
                continue
        parts[index] = _rebase_text(part, root, copy)
    return os.pathsep.join(parts)


@contextmanager
def _at_copy(root: Path, copy: Path):
    """Re-root cwd and inherited child paths for this synchronous CLI call."""
    cwd = Path.cwd()
    changes = {}
    for key, value in os.environ.items():
        _rebase_text(key, root, None)
        rebased = _copy_env_value(value, root, copy)
        if rebased != value:
            changes[key] = rebased
    changes["PWD"] = str(copy)
    changes["GIT_CEILING_DIRECTORIES"] = str(copy.parent)
    # The parent recorder holds the live log. Never hand its path to an agent.
    if "CODD_AI_USAGE_LOG" in os.environ:
        changes["CODD_AI_USAGE_LOG"] = ""
    previous = {key: os.environ.get(key) for key in changes}
    try:
        os.chdir(copy)
        os.environ.update(changes)
        yield
    finally:
        os.chdir(cwd)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _changed(before, after):
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def _journal_changes(changes, before, after, journals):
    """Allow the journals and any newly-created ordinary parent directories only."""
    allowed = set()
    for path in journals & changes:
        if path in after and stat.S_ISREG(after[path][0]):
            allowed.add(path)
            allowed.update(parent for parent in path.parents
                           if parent not in before and parent in after and stat.S_ISDIR(after[parent][0]))
    return allowed


def _work_extras(before, after, changes, targets):
    records = _RECORDS | _GIT_RECORDS
    # Capture stages content in independent Git objects. A hook, a ref or
    # configuration file written by the agent still counts as an extra.
    objects = {
        name for name in changes if name not in before and name in after
        and re.fullmatch(r"\.git/objects/[0-9a-f]{2}/(?:[0-9a-f]{38}|[0-9a-f]{62})", name.as_posix())
    }
    return changes - targets - _journal_changes(changes, before, after, records | objects)


def _usage_destination(root, config, paths, tasks):
    usage_config = deepcopy(config)
    if not (usage_config.get("ai") or {}).get("usage_log"):
        usage_config.setdefault("ai", {})["usage_log"] = ".codd/ai-usage.jsonl"
    log = active_usage_log() or resolve_usage_log(usage_config, root)
    if log is None or log.is_relative_to(root / ".git"):
        raise _Declined("P2: usage log unavailable")
    reserved = paths | _RECORDS | {Path("codd/codd.yaml"), Path(".codd/codd.yaml")}
    for task in tasks:
        reserved.update(Path(item) for item in (task.design_node, *task.expected_outputs, *(task.output_paths or ())))
    if any(log == (root / rel).resolve() for rel in reserved):
        raise _Declined("P2: usage log overlaps a project input")
    parent = next(directory for directory in log.parents if directory.exists())
    if not parent.is_dir() or not os.access(log if log.exists() else parent, os.W_OK):
        raise _Declined("P2: usage log not writable")
    if log.exists():
        if not log.is_file() or log.stat().st_nlink != 1 or summarize_usage(log)[1]:
            raise _Declined("P2: existing destination is not a dedicated usage log")
        if log.is_relative_to(root) and not log.stat().st_size and log != root / ".codd/ai-usage.jsonl":
            raise _Declined("P2: empty project file cannot be identified as a usage log")
    return usage_config, log


@dataclass(frozen=True)
class _Replacement:
    data: bytes
    mode: int
    mtime_ns: int

    def write(self, path: Path) -> None:
        path.write_bytes(self.data)
        path.chmod(stat.S_IMODE(self.mode))
        # Time-based build caches must see the same replacement at both roots.
        os.utime(path, ns=(path.stat().st_atime_ns, self.mtime_ns))


def _restore_target(dest: Path, old: Path, original: _Replacement) -> None:
    if dest.parent.resolve() != dest.parent:
        raise OSError("target parent was redirected")
    try:
        os.replace(old, dest)
    except OSError:
        # Staging has normally been removed before the live check. Recreate
        # the target itself, even if the checker removed or relinked it.
        dest.unlink(missing_ok=True)
        dest.write_bytes(original.data)
        dest.chmod(stat.S_IMODE(original.mode))
    mode = dest.lstat().st_mode
    if (not stat.S_ISREG(mode) or (mode ^ original.mode) & 0o111
            or dest.read_bytes() != original.data):
        raise OSError("restored content or executable bits differ")


def _publish(root: Path, candidates: dict[Path, _Replacement], confirm: Callable[[], bool]) -> None:
    with ExitStack() as staging_dirs:
        files = []
        for rel, replacement in candidates.items():
            dest = root / rel
            meta = dest.stat()
            original = _Replacement(dest.read_bytes(), meta.st_mode, meta.st_mtime_ns)
            staging = Path(staging_dirs.enter_context(tempfile.TemporaryDirectory(dir=dest.parent)))
            new, old = staging / "new", staging / "old"
            new.touch()
            shutil.copystat(dest, new)
            replacement.write(new)
            os.link(dest, old)
            files.append((dest, new, old, original))
        published = []
        try:
            for dest, new, old, original in files:
                os.replace(new, dest)
                published.append((dest, old, original))
            staging_dirs.close()
            if not confirm():
                raise _Declined("P3: live recheck failed")
        except BaseException:
            failures = []
            for dest, old, original in reversed(published):
                try:
                    _restore_target(dest, old, original)
                except Exception as exc:
                    failures.append(f"{dest.relative_to(root)} ({exc})")
            if failures:
                raise OwnerFirstRestoreError("could not restore " + ", ".join(failures))
            for _, new, _, _ in files:
                if new.parent.exists():
                    shutil.rmtree(new.parent)
            raise


def try_owner_first(
    root: Path, tasks: Sequence[ImplementTaskRef], targets: Iterable[str | Path], feedback: str,
    config: dict[str, Any], *, reimplement: Callable, validate: Callable,
    eligible_tasks: Sequence[ImplementTaskRef] | None = None, skip_all_eligible: bool = False,
    echo: Callable[[str], None] = print, ai_command: str | None = None,
) -> bool:
    """Try each elected owner once, then accept only the independently checked files."""
    if not owner_first_retry_enabled(config):
        return False
    root = root.resolve()
    try:
        paths = {(root / Path(raw)).relative_to(root) for raw in targets}
        if not paths or any(not p.parts or ".." in p.parts or p.parts[0] in {".git", ".codd"} for p in paths):
            return False
        eligible = tasks if eligible_tasks is None else eligible_tasks
        for item in (tasks, config, ai_command):
            _rebase_text(str(item), root, None)
        if not _portable(config) or not _portable(ai_command, command=True):
            return False
        for task in tasks:
            for item in (task.design_node, *(task.output_paths or ()), *task.expected_outputs):
                if Path(item).is_absolute() or ".." in Path(item).parts:
                    return False
        routing_config = deepcopy(config)
        routing_config.setdefault("project", {}).setdefault("root", str(root))
        owners = _owners(root, tasks, paths, routing_config, eligible, skip_all_eligible)
        if not owners:
            return False
    except Exception:
        return False  # P1: a failed lookup is the ordinary, unmodified retry.

    echo(f"[greenfield] owner-first retry: trying {', '.join(t.task_id for t in owners)} on a copy")
    try:
        temp_parent = Path(tempfile.gettempdir()).resolve()
        interpreter = Path(sys.executable)
        if (temp_parent.is_relative_to(root) or threading.active_count() != 1
                or interpreter.is_relative_to(root) or interpreter.resolve().is_relative_to(root)):
            raise _Declined("P2: cannot isolate the current execution paths")
        if any(p and (Path(p).resolve() == root.parent or Path(p).resolve().is_relative_to(root))
               for p in sys.path):
            raise _Declined("P2: interpreter imports from the live project")
        before = _inventory(root, original=root)
        _check_git(root, before)
        usage_config, log = _usage_destination(root, config, paths, tasks)
        journals = {log.relative_to(root)} if log.is_relative_to(root) else set()
        with tempfile.TemporaryDirectory(prefix="codd-owner-") as temp:
            work = Path(temp) / "work" / root.name
            judge = Path(temp) / "judge" / root.name
            shutil.copytree(root, work, symlinks=True)
            if _inventory(work) != before:
                raise _Declined("P2: project changed during copy")
            with recorded_usage_session(usage_config, root), _at_copy(root, work):
                completed = reimplement(work, owners, _rebase_text(feedback, root, work), deepcopy(config))
                if set(completed) != {task.task_id for task in owners}:
                    raise _Declined("P5: owner did not complete")
            after = _inventory(work)
            changes = _changed(before, after)
            extras = _work_extras(before, after, changes, paths)
            if extras:
                raise _Declined(f"P4: other artifact changed ({min(extras)})")
            candidates = {}
            for path in sorted(paths):
                if path not in after or not stat.S_ISREG(after[path][0]) or (work / path).resolve() != work / path:
                    raise _Declined("P4: replacement is not a regular file")
                content = (work / path).read_bytes()
                if os.fsencode(temp) in content or os.fsencode(root) in content:
                    raise _Declined("P3: replacement depends on a specific project path")
                candidates[path] = _Replacement(content, after[path][0], (work / path).stat().st_mtime_ns)
            if not changes.intersection(paths):
                raise _Declined("P3: no replacement")
            shutil.copytree(root, judge, symlinks=True)
            live = _inventory(judge)
            changed = _changed(before, live)
            if changed - _journal_changes(changed, before, live, journals):
                raise _Declined("P3: live project changed during the trial")
            for rel, replacement in candidates.items():
                replacement.write(judge / rel)
            inputs = dict(live)
            inputs.update((rel, after[rel]) for rel in candidates)
            if not validate(judge, deepcopy(config)):
                raise _Declined("P3: recheck failed")
            checked = _inventory(judge)
            changes = _changed(inputs, checked)
            if changes - _journal_changes(changes, inputs, checked, _RECORDS):
                raise _Declined("P3: recheck changed project files")
            if _inventory(root) != live:
                raise _Declined("P5: live project changed before publication")
        _publish(root, candidates, lambda: validate(root, deepcopy(config)))
    except OwnerFirstRestoreError as exc:
        echo(f"[greenfield] owner-first retry: {exc}; stopping")
        raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, _Declined) else f"P5: {type(exc).__name__}"
        echo(f"[greenfield] owner-first retry: rejected ({reason})")
        return False
    echo("[greenfield] owner-first retry: accepted")
    return True
