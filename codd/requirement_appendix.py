"""Read the editable elicit appendix as requirements, independently of the document graph."""

from __future__ import annotations

import re
import stat
from codecs import BOM_UTF8
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from codd.path_safety import resolve_project_path


PROVISIONAL_DECISIONS_PATH = Path("docs/requirements/provisional-decisions.md")
DECISION_LOCK_PATH = Path(".codd/elicit/provisional-decisions.lock")
DOCUMENT_MARKER = "<!-- codd:provisional-decisions -->"
DECISION_STATUS = "- Status: AI provisional (human-editable)"
APPENDIX_GUIDANCE = (
    "An existing CoDD appendix is read without the flag. Delete an entire entry to have it "
    "decided again if rediscovered or still pending on the next run with the flag."
)
_ENTRY_MARKER = re.compile(rb"<!-- codd:provisional-decision ((?:[a-f0-9]{2})+)(?: (?:[a-f0-9]{64}|-))? -->")
_FENCE = re.compile(rb"^ {0,3}(`{3,}|~{3,})(.*)$")


def parse_entry_marker(line: bytes) -> str | None:
    marker = _ENTRY_MARKER.fullmatch(line)
    if marker is None:
        return None
    try:
        return bytes.fromhex(marker[1].decode("ascii")).decode("utf-8", errors="surrogatepass")
    except UnicodeDecodeError:
        return None


def markdown_outside_fences(content: bytes) -> tuple[list[bytes], bytes]:
    """Hide fenced entry markers while retaining fenced text as body content."""
    visible = []
    fence = b""
    for line in content.removeprefix(BOM_UTF8).splitlines():
        delimiter = _FENCE.fullmatch(line)
        if fence:
            if delimiter and delimiter[1].startswith(fence) and not delimiter[2].strip():
                fence = b""
        elif delimiter and (delimiter[1].startswith(b"~") or b"`" not in delimiter[2]):
            fence = delimiter[1]
        else:
            visible.append(line)
            continue
        visible.append(line if line.startswith(b"  ") else b"  " + line)
    return visible, fence


def is_managed_appendix(project_root: Path | str, content: bytes) -> bool:
    """Recognize a generated appendix even after its body has been rewritten."""
    # An empty lock is also created by review-only runs. A nonempty lock records
    # that this project actually wrote an appendix, without tracking any entries.
    lock_path = resolve_project_path(project_root, DECISION_LOCK_PATH)
    if lock_path is not None and lock_path.is_file() and lock_path.stat().st_size:
        return True
    marker = DOCUMENT_MARKER.encode("ascii")
    lines, _ = markdown_outside_fences(content)
    for line in lines:
        line = line.rstrip(b" \t")
        if line == marker or parse_entry_marker(line) is not None:
            return True
    return False


class UnreadableRequirementAppendix(ValueError):
    """A managed requirements document cannot be read as UTF-8."""


def _read_managed_appendix_bytes(project_root: Path | str) -> bytes | None:
    path = Path(project_root) / PROVISIONAL_DECISIONS_PATH
    if path.is_symlink() or not path.is_file():
        return None
    if resolve_project_path(project_root, PROVISIONAL_DECISIONS_PATH) is None:
        return None
    content = path.read_bytes()
    return content if is_managed_appendix(project_root, content) else None


def read_requirement_appendix(project_root: Path | str) -> str | None:
    """Read the managed requirements appendix."""
    content = _read_managed_appendix_bytes(project_root)
    if content is None:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        path = Path(project_root) / PROVISIONAL_DECISIONS_PATH
        raise UnreadableRequirementAppendix(f"Cannot read {path} as UTF-8: {exc}") from exc


def is_requirement_appendix(project_root: Path, relative_path: str) -> bool:
    """Check whether the appendix is exempt from standalone document graph metadata."""
    return (
        relative_path == PROVISIONAL_DECISIONS_PATH.as_posix()
        and read_requirement_appendix(project_root) is not None
    )


def with_requirement_appendix(prompt: str, project_root: Path) -> str:
    """Include the current requirements even when an artifact only depends on another design."""
    content = read_requirement_appendix(project_root)
    if content is None:
        return prompt
    path = PROVISIONAL_DECISIONS_PATH.as_posix()
    body = content if content.endswith("\n") else content + "\n"
    return (
        f"--- BEGIN REQUIREMENT {path} (appendix to the project requirements) ---\n"
        f"{body}--- END REQUIREMENT {path} ---\n\n{prompt}"
    )


def protect_requirement_appendix(
    project_root: Path, destinations: list[Path], *, removing: bool = False,
) -> None:
    """Reject a write to the managed appendix or removal of its containing directory."""
    if not destinations or read_requirement_appendix(project_root) is None:
        return
    appendix = (project_root / PROVISIONAL_DECISIONS_PATH).resolve()
    for destination in destinations:
        path = resolve_project_path(project_root, destination)
        protected = path == appendix or (removing and path in appendix.parents)
        if not protected and not removing and path is not None:
            try:
                protected = path.samefile(appendix)
            except FileNotFoundError:
                pass
        if protected:
            action = "remove" if removing else "overwrite"
            raise RequirementAppendixWriteError(
                f"Cannot {action} the managed requirements appendix: {PROVISIONAL_DECISIONS_PATH.as_posix()}"
            )


class RequirementAppendixWriteError(ValueError):
    """An implementation attempted to change the managed requirements appendix."""


_appendix_snapshot_root: ContextVar[Path | None] = ContextVar("appendix_snapshot_root", default=None)


def has_requirement_appendix_snapshot(project_root: Path) -> bool:
    root = _appendix_snapshot_root.get()
    return root is not None and root == project_root.resolve()


@contextmanager
def preserve_requirement_appendix(project_root: Path) -> Iterator[None]:
    """Restore any change to the managed appendix before rejecting the attempt."""
    original = _read_managed_appendix_bytes(project_root)
    if original is None:
        yield
        return
    path = project_root / PROVISIONAL_DECISIONS_PATH
    parent = path.parent.resolve()
    token = _appendix_snapshot_root.set(project_root.resolve())
    try:
        yield
    finally:
        _appendix_snapshot_root.reset(token)
        message = f"AI attempted to write the managed requirements appendix: {PROVISIONAL_DECISIONS_PATH.as_posix()}"
        if path.parent.resolve() != parent:
            raise RequirementAppendixWriteError(f"{message} (the parent directory was replaced)")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            mode = 0
        if not stat.S_ISREG(mode) or path.read_bytes() != original:
            if stat.S_ISDIR(mode):
                path.rmdir()
            elif mode and not stat.S_ISREG(mode):
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
            raise RequirementAppendixWriteError(message)


def implementation_step_outputs(outputs: object, hint: object) -> list[Path]:
    paths = [
        Path(path.strip()) for path in outputs if isinstance(path, str) and path.strip()
    ] if isinstance(outputs, (list, tuple)) else []
    if isinstance(hint, str) and hint.strip():
        paths.append(Path(hint.strip()))
    return paths
