"""CoDD implementer - direct design document to output path generation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
import warnings
from typing import Any, Callable

import codd.generator as generator_module
from codd.generator import DependencyDocument, _load_project_config, _normalize_conventions
from codd.project_types import ProjectCapabilities
from codd.scanner import _extract_frontmatter, build_document_node_path_map
import unicodedata


FILE_BLOCK_RE = re.compile(r"^=== FILE: (?P<path>.+?) ===\s*$", re.MULTILINE)
# Repo-root infrastructure artifacts (implement.root_artifact_patterns):
# AI-emitted paths matching these are written at the PROJECT ROOT instead of
# being confined under (or dropped for being outside) the task output paths.
# A CI workflow rerooted under src/<task>/.github/ never runs — observed in
# the 2026-06 real-AI greenfield dogfood, where the final `codd check`
# ci_health gate went red because the workflow landed inside the task dir.
DEFAULT_ROOT_ARTIFACT_PATTERNS: tuple[str, ...] = (
    ".github/**",
    ".gitlab-ci.yml",
    "pyproject.toml",
    "setup.cfg",
    "package.json",
    "Dockerfile*",
    "docker-compose*",
    ".gitignore",
    "README*",
    "Makefile",
    "LICENSE*",
)
LANGUAGE_EXT_MAP: dict[str, tuple[str, ...]] = {
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx"),
    "python": (".py",),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "kotlin": (".kt",),
    "swift": (".swift",),
    "cpp": (".cpp", ".cc", ".h"),
    "c": (".c", ".h"),
    "csharp": (".cs",),
    "ruby": (".rb",),
}
LANGUAGE_ALIASES = {
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "py": "python",
    "rs": "rust",
    "golang": "go",
    "c++": "cpp",
    "cc": "cpp",
    "c#": "csharp",
    "cs": "csharp",
}
LANGUAGE_DISPLAY_NAMES = {
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "python": "Python",
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "cpp": "C++",
    "c": "C",
    "csharp": "C#",
    "ruby": "Ruby",
}
LANGUAGE_CODE_FENCE_MAP = {
    "typescript": "ts",
    "javascript": "js",
    "python": "python",
    "rust": "rust",
    "go": "go",
    "java": "java",
    "kotlin": "kotlin",
    "swift": "swift",
    "cpp": "cpp",
    "c": "c",
    "csharp": "csharp",
    "ruby": "ruby",
}
COMMENT_PREFIX_BY_SUFFIX = {
    ".ts": "//",
    ".tsx": "//",
    ".js": "//",
    ".jsx": "//",
    ".py": "#",
    ".rs": "//",
    ".go": "//",
    ".java": "//",
    ".kt": "//",
    ".swift": "//",
    ".cpp": "//",
    ".cc": "//",
    ".h": "//",
    ".c": "//",
    ".cs": "//",
    ".rb": "#",
}
UI_FILE_EXTENSIONS = {".tsx", ".jsx", ".vue", ".svelte", ".swift", ".kt", ".dart"}
SCREEN_FLOW_PROMPT_LIMIT = 8000
_DEFAULT_GUARD_FILES = ["middleware.ts", "middleware.js"]
_SKIP_GENERATION_RE = re.compile(
    r"(?mi)^\s*(?:[-*]\s*)?skip_generation\s*:\s*true\s*$",
)
_ROUTE_TOKEN_RE = re.compile(r"(?<![:\w])/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_ROUTE_HOME_KEYWORDS = {"home", "homepage", "landing", "root", "top", "top page"}
_ROUTE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_FRAMEWORK_KEYWORDS = {
    "angular",
    "astro",
    "next",
    "next.js",
    "nextjs",
    "nuxt",
    "react",
    "remix",
    "svelte",
    "vue",
}
_UI_TASK_KEYWORDS = frozenset(
    {
        "component",
        "frontend",
        "layout",
        "login",
        "page",
        "route",
        "screen",
        "signup",
        "ui",
        "ux",
        "view",
        "widget",
        "ログイン",
        "画面",
    }
    - _FRAMEWORK_KEYWORDS
)
UI_TASK_KEYWORDS = _UI_TASK_KEYWORDS
_WRAPPER_TASK_KEYWORDS = frozenset(
    {
        "page wrapper",
        "root page",
        "thin wrapper",
        "wrapper",
        "ページラッパー",
        "ラッパー",
    }
)
EXPORT_TYPE_RE = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:type|interface|enum)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_CLASS_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_FUNCTION_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_VALUE_RE = re.compile(
    r"^\s*export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_NAMED_BLOCK_RE = re.compile(
    r"^\s*export\s+(?P<type_prefix>type\s+)?{\s*(?P<body>[^}]+)\s*}(?:\s+from\s+['\"].+['\"])?\s*;?",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ImplementSpec:
    design_node: str
    output_paths: list[str]
    dependency_design_nodes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        design_node = str(self.design_node).strip()
        if not design_node:
            raise ValueError("design_node is required")
        output_paths = _ordered_unique([str(item).strip() for item in self.output_paths if str(item).strip()])
        if not output_paths:
            raise ValueError("output_paths must contain at least one path")
        dependencies = _ordered_unique(
            [str(item).strip() for item in self.dependency_design_nodes if str(item).strip()]
        )
        object.__setattr__(self, "design_node", design_node)
        object.__setattr__(self, "output_paths", output_paths)
        object.__setattr__(self, "dependency_design_nodes", dependencies)

    @property
    def task_id(self) -> str:
        return self.design_node

    @property
    def title(self) -> str:
        return Path(self.design_node).stem.replace("_", " ").replace("-", " ").strip() or self.design_node

    @property
    def summary(self) -> str:
        return f"Implement {self.design_node}"

    @property
    def module_hint(self) -> str:
        return ", ".join(self.output_paths)

    @property
    def deliverable(self) -> str:
        return ", ".join(self.output_paths)

    @property
    def output_dir(self) -> str:
        return self.output_paths[0]

    @property
    def dependency_node_ids(self) -> list[str]:
        return list(self.dependency_design_nodes)

    @property
    def task_context(self) -> str:
        return ""


@dataclass(frozen=True)
class DesignContext:
    node_id: str
    path: Path
    content: str
    depends_on: list[dict[str, Any]] = field(default_factory=list)
    conventions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ImplementationResult:
    design_node: str
    output_paths: list[Path]
    generated_files: list[Path]
    error: str | None = None

    @property
    def task_id(self) -> str:
        return self.design_node

    @property
    def task_title(self) -> str:
        return Path(self.design_node).stem.replace("_", " ").replace("-", " ").strip() or self.design_node

    @property
    def output_dir(self) -> Path:
        return self.output_paths[0]


# Cap on how many distinct offending characters we enumerate per file in the
# retry feedback — enough to steer the model, bounded so the prompt stays small.
_NONASCII_FEEDBACK_CAP = 5


def _describe_nonascii_code_chars(content: str, *, lineno: int | None) -> list[str]:
    """Enumerate non-ASCII characters the parser would treat as code tokens.

    Best-effort and cheap (no LLM): scans ``content`` for characters outside
    ASCII, reporting each with its line, the character itself, its U+XXXX
    codepoint, and (when free, via ``unicodedata``) its Unicode name. The line
    the parser flagged (``lineno``) is reported FIRST so the model sees the
    exact failing position; remaining non-ASCII characters elsewhere in the
    file follow (capped). Deterministic and language-neutral — the caller frames
    the ASCII directive; this only surfaces the offending codepoints.

    Note: this cannot perfectly distinguish a code position from inside a
    string/comment on syntactically-broken input, so it lists every non-ASCII
    character; the directive makes the rule explicit (ASCII for code tokens,
    non-ASCII only inside literals/comments).
    """
    lines = content.splitlines()

    def _describe_char(line_no: int, ch: str) -> str:
        codepoint = f"U+{ord(ch):04X}"
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = None
        suffix = f", {name}" if name else ""
        return f"line {line_no}: {ch!r} ({codepoint}{suffix})"

    described: list[str] = []
    seen: set[tuple[int, str]] = set()

    def _scan_line(line_no: int) -> None:
        if line_no < 1 or line_no > len(lines):
            return
        for ch in lines[line_no - 1]:
            if ord(ch) > 127 and (line_no, ch) not in seen:
                seen.add((line_no, ch))
                described.append(_describe_char(line_no, ch))

    # Parser-flagged line first (the precise failing position), then the rest.
    if lineno is not None:
        _scan_line(lineno)
    for line_no in range(1, len(lines) + 1):
        if len(described) >= _NONASCII_FEEDBACK_CAP:
            break
        if line_no == lineno:
            continue
        _scan_line(line_no)

    return described[:_NONASCII_FEEDBACK_CAP]


class ImplementSyntaxGateError(RuntimeError):
    """AI-produced file payload(s) failed the write-time syntax gate.

    Raised BEFORE anything is written to disk: a syntactically broken file is
    never silently persisted as "task done". ``failures`` holds
    ``(relative_path, error)`` pairs describing what the AI produced;
    ``payloads`` maps each path to the rejected content so the retry feedback
    can pinpoint offending characters (codepoints) without re-parsing.
    """

    def __init__(
        self,
        failures: list[tuple[str, str]],
        *,
        payloads: dict[str, str] | None = None,
        confusable_failures: list[tuple[str, str]] | None = None,
    ) -> None:
        self.failures = list(failures)
        self.payloads = dict(payloads or {})
        # Confusable findings are a SEPARATE class from parse failures: the file
        # parses, but a code-position identifier mixes scripts (homoglyph slip).
        # Tracked alongside so one error type drives the same atomic-write +
        # bounded-retry machinery as the syntax gate.
        self.confusable_failures = list(confusable_failures or [])
        all_failures = self.failures + self.confusable_failures
        details = "; ".join(f"{path}: {error}" for path, error in all_failures)
        super().__init__(
            f"syntax gate rejected {len(all_failures)} generated file(s): {details}"
        )

    @property
    def all_failures(self) -> list[tuple[str, str]]:
        """Parse failures and confusable failures, for naming the rejected files."""
        return self.failures + self.confusable_failures

    def feedback_message(self) -> str:
        lines = [
            "The previous implementation attempt was rejected and was NOT written.",
            "Regenerate ALL files for this task so that every file is correct:",
        ]
        for path, error in self.failures:
            lines.append(f"- file {path} is {error}")
            content = self.payloads.get(path)
            if not content:
                continue
            offending = _describe_nonascii_code_chars(
                content, lineno=_parsed_error_lineno(error)
            )
            if offending:
                joined = "; ".join(offending)
                lines.append(
                    f"  non-ASCII character(s) found in {path}: {joined}. "
                    "These are invalid in code position."
                )
        for path, error in self.confusable_failures:
            lines.append(f"- file {path} {error}")
        lines.append(
            "Use ASCII for ALL code tokens (identifiers, operators, punctuation, "
            "statement terminators); replace typographic/full-width punctuation -- "
            "em dash (—, U+2014), full-width period (。, U+3002), full-width "
            "comma (，, U+FF0C), smart quotes (“”‘’), etc. -- "
            "with their ASCII equivalents (-, ., ,, \", '). "
            "Non-ASCII is allowed ONLY inside string/text literals and comments."
        )
        if self.confusable_failures:
            lines.append(
                "At least one identifier MIXES SCRIPTS (e.g. a Cyrillic or Greek "
                "letter that looks like an ASCII letter -- a homoglyph -- inside an "
                "otherwise-ASCII name). This parses but is a different name at "
                "runtime (NameError / silent mismatch). Use ASCII Latin letters "
                "for ALL code identifiers; only the listed code positions are "
                "affected -- do NOT change text inside string literals or comments."
            )
        return "\n".join(lines)


def _parsed_error_lineno(error: str) -> int | None:
    """Pull the ``line N`` the format parsers embed in their error strings."""
    match = re.search(r"\bline (\d+)\b", error)
    return int(match.group(1)) if match else None


def _syntax_gate_enabled(config: dict[str, Any]) -> bool:
    """``implement.syntax_gate`` — default ON (see defaults.yaml)."""
    section = config.get("implement")
    if isinstance(section, dict) and "syntax_gate" in section:
        return bool(section["syntax_gate"])
    return True


# Total implement attempts allowed when the syntax gate is ON: one initial
# generation plus bounded corrective retries. Default 3 (was 2) — a model primed
# by non-ASCII context can slip typographic punctuation into code more than once,
# and the actionable feedback needs an extra shot to land a clean file. Bounded
# on purpose: a genuinely unfixable file still fails honestly, nothing written.
DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS = 3


def _syntax_gate_max_attempts(config: dict[str, Any]) -> int:
    """``implement.syntax_gate_max_attempts`` — total attempts when the gate is ON.

    Defaults to :data:`DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS`. Always at least 1
    (the initial attempt); a configured value below 1 or a non-integer falls
    back to the default. NEVER unbounded — the gate must still fail loudly on a
    permanently-invalid file.
    """
    section = config.get("implement")
    if isinstance(section, dict) and "syntax_gate_max_attempts" in section:
        raw = section["syntax_gate_max_attempts"]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS
        if value >= 1:
            return value
        return DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS
    return DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS


class NoUsableGeneratedFiles(RuntimeError):
    """One implement attempt yielded NO usable generated files (transiently).

    Raised inside :func:`Implementer._run_implementation_generation_attempt`
    when a single prompt->invoke->write pass produces nothing writable for a
    reason that a re-issue of the SAME effective prompt can clear:

    - (a) the AI returned empty output, or a file-writing agent produced no
      (readable) file changes;
    - (b) the AI emitted file block(s) but every one was invalid / out of scope
      (``_write_generated_files`` ValueError);
    - (c) the payload parsed but filtered down to zero usable files.

    This is DISTINCT from :class:`ImplementSyntaxGateError` (files were produced
    but failed deterministic syntax/confusable validation) and from
    ``skip_generation: true`` (an explicit, sanctioned 0-file success). The
    supervisor loop in :func:`Implementer.run_implement` retries this a bounded
    number of times and then raises the SAME ``_zero_generated_files_error`` as
    a no-retry world would — never a softer outcome.
    """


# A transient no-usable-output AI response on a single implement attempt is
# re-issued a bounded number of times before the zero-files hard-fail gate (the
# greenfield dogfood finding: a single empty/unparseable codex response exit-0
# hard-failed a multi-hour autopilot run, yet re-running the identical call
# succeeded). SEPARATE from the transient-transport knob in ai_invoke
# (TRANSIENT_AUTO_RETRIES) on purpose: transport drop and no-usable-file output
# are different failure classes; tying them to one knob makes later tuning
# unsafe. Numerically aligned with that precedent (3 / 1.5s).
DEFAULT_NO_USABLE_FILE_RETRIES = 3
#: Short backoff (seconds) between no-usable-output retries; index-scaled
#: (1.5s / 3.0s / 4.5s for retries 1/2/3).
DEFAULT_NO_USABLE_FILE_BACKOFF_SECONDS = 1.5


def _no_usable_file_retries(config: dict[str, Any]) -> int:
    """``implement.no_usable_file_retries`` — retries on a no-usable-output attempt.

    Defaults to :data:`DEFAULT_NO_USABLE_FILE_RETRIES` (so 4 total attempts).
    A configured value below 0 or a non-integer falls back to the default.
    Bounded on purpose: a genuinely insufficient design still fails honestly
    with the same zero-files error, nothing written.
    """
    section = config.get("implement")
    if isinstance(section, dict) and "no_usable_file_retries" in section:
        raw = section["no_usable_file_retries"]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_NO_USABLE_FILE_RETRIES
        if value >= 0:
            return value
        return DEFAULT_NO_USABLE_FILE_RETRIES
    return DEFAULT_NO_USABLE_FILE_RETRIES


# The file-writing-agent (e.g. codex exec) and the text-out path both surface a
# "no usable output" condition as a ValueError. An empty-output ValueError and
# the file-writing-agent "did not produce any (readable) file changes" capture
# errors are the SAME no-usable class for implement: re-issuing the identical
# call can clear them. Matched conservatively from the message text so genuine
# permanent errors (missing binary, auth/billing/validation) never match.
_NO_USABLE_OUTPUT_MARKERS: tuple[str, ...] = (
    "empty output",
    "did not produce any file changes",
    "did not produce any readable file changes",
)


def _is_no_usable_output_error(exc: ValueError) -> bool:
    detail = str(exc).casefold()
    return any(marker in detail for marker in _NO_USABLE_OUTPUT_MARKERS)


def _payload_syntax_error(relative_path: str, content: str) -> str | None:
    """Best-effort syntax validation of one AI-produced payload.

    Validates ONLY what the AI produced for files the implementer is about to
    write — never pre-existing project files. Keyed purely by file extension
    and the parsers the standard library (or codd's existing hard
    dependencies) already ship: Python (``ast``), JSON, YAML, TOML. Formats
    without such a parser (TypeScript, JavaScript, Go, ...) are skipped on
    purpose — the verify-stage gate is their backstop. Cheap, deterministic,
    no LLM calls. Returns a human/AI-readable error or ``None`` when valid.
    """
    suffix = PurePosixPath(relative_path).suffix.lower()
    # A single leading UTF-8 BOM (U+FEFF) is valid in real source: CPython loads
    # BOM-prefixed .py via utf-8-sig and JSON parsers accept a BOM-prefixed
    # document, so a payload that imports/loads cleanly must not be rejected here.
    # The strip is validation-only; what gets written to disk is unaffected.
    parse_content = content[1:] if content[:1] == "﻿" else content
    if suffix == ".py":
        import ast

        try:
            ast.parse(parse_content)
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return f"not valid Python ({location}: {exc.msg})"
    elif suffix == ".json":
        try:
            json.loads(parse_content)
        except json.JSONDecodeError as exc:
            return f"not valid JSON (line {exc.lineno}: {exc.msg})"
    elif suffix in {".yaml", ".yml"}:
        import yaml

        try:
            # safe_load accepts only ONE document; a valid ``---``-separated
            # multi-document file (k8s manifests, multi-resource CI) is correct
            # input. safe_load_all parses every document; consume the generator
            # so each one is actually validated. Malformed YAML still raises.
            list(yaml.safe_load_all(content))
        except yaml.YAMLError as exc:
            return f"not valid YAML ({exc})"
    elif suffix == ".toml":
        # tomllib is stdlib from 3.11; tomli is its <3.11 backport and a core
        # dependency there. Skip only when neither exists (broken install) —
        # mirrors the verify-stage parser chain.
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:  # pragma: no cover - broken install
                return None
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            return f"not valid TOML ({exc})"
    return None


# ---------------------------------------------------------------------------
# Confusable / non-ASCII-in-code detector (layer 2, sibling of the syntax gate)
#
# The syntax gate (ast.parse) catches non-ASCII that BREAKS parsing — e.g. a
# full-width period in a code position. It cannot catch a homoglyph in a
# syntactically-VALID position: a Cyrillic ``е`` (U+0435) or ``а`` (U+0430)
# substituted for ASCII ``e``/``a`` INSIDE an identifier (``іd`` looks like
# ``id`` but is a different name). The file parses fine, may even pass tests
# written with the same wrong char, but is semantically broken — a false-green.
# Current-generation models (Sonnet AND Opus) emit these intermittently.
#
# Precision is the whole game here (false positives are the main risk), so the
# detector is deliberately narrow:
#   * It tokenizes (Python: stdlib ``tokenize``) and inspects ONLY code-position
#     tokens — NAME/identifier and OP/operator tokens. STRING, COMMENT, and
#     f-string text tokens (FSTRING_START/MIDDLE/END) are skipped ENTIRELY, so
#     non-ASCII inside string literals and comments (Japanese UI copy, etc.) is
#     never flagged. That is the critical false-positive guard.
#   * In NAME tokens it flags only MIXED-SCRIPT identifiers: an identifier that
#     mixes ASCII-Latin letters with Cyrillic or Greek letters. That is almost
#     always a homoglyph slip (near-zero false-positive). A single-script
#     non-Latin identifier that the language genuinely allows (a fully-Greek or
#     fully-CJK name) is NOT flagged — only the Latin+Cyrillic/Greek mix is.
#   * In OP tokens it flags ANY non-ASCII (full-width punctuation etc.); most of
#     those already break the parser, this is a cheap belt-and-braces backstop.
# Languages without a cheap stdlib tokenizer are skipped on purpose (Python is
# where the observed failure lives; the syntax gate is the backstop elsewhere) —
# correctness over coverage: skip rather than risk a false positive.

# Unicode scripts that carry ASCII-Latin confusable homoglyphs. Detected via the
# first word of ``unicodedata.name(ch)`` (e.g. "CYRILLIC SMALL LETTER IE"),
# which avoids a heavyweight Unicode-database dependency.
_CONFUSABLE_SCRIPTS = ("CYRILLIC", "GREEK")


def _char_script(ch: str) -> str | None:
    """The Unicode script name (first word of the char's Unicode name).

    ``unicodedata.name`` yields e.g. ``"CYRILLIC SMALL LETTER IE"`` — the first
    token is the script. Returns ``None`` for unnamed characters.
    """
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return None


def _is_ascii_latin_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def _confusable_scripts_in_identifier(name: str) -> set[str]:
    """Scripts from :data:`_CONFUSABLE_SCRIPTS` present in a MIXED-script name.

    Returns the offending scripts ONLY when ``name`` mixes ASCII-Latin letters
    with one or more confusable (Cyrillic/Greek) letters — the high-signal
    homoglyph pattern. A pure-ASCII name, or a name with NO ASCII-Latin letter
    at all (a legitimately single-script non-Latin identifier), returns an empty
    set: not flagged.
    """
    has_ascii_latin = any(_is_ascii_latin_letter(ch) for ch in name)
    if not has_ascii_latin:
        return set()
    offending: set[str] = set()
    for ch in name:
        if ch.isascii():
            continue
        script = _char_script(ch)
        if script in _CONFUSABLE_SCRIPTS:
            offending.add(script)
    return offending


def _describe_confusable_char(line_no: int, ch: str, *, identifier: str) -> str:
    codepoint = f"U+{ord(ch):04X}"
    script = _char_script(ch) or "UNKNOWN-SCRIPT"
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = None
    suffix = f", {name}" if name else ""
    return (
        f"line {line_no}: identifier {identifier!r} contains {ch!r} "
        f"({codepoint}{suffix}; {script} script)"
    )


def _confusable_findings(content: str) -> list[str]:
    """Confusable non-ASCII characters in CODE positions of Python ``content``.

    This is the Python engine: it tokenizes ``content`` with the stdlib
    ``tokenize`` module. Language routing (which payloads reach here) is the
    caller's job — :func:`_confusable_code_error` gates on the file suffix /
    project language; non-Python formats never get this far (the syntax gate is
    their backstop, and a line-level heuristic risks false positives on
    string/comment spans). Returns human/AI-readable descriptions (bounded by
    :data:`_NONASCII_FEEDBACK_CAP`); an empty list means nothing suspicious.

    Only NAME tokens (checked for mixed-script identifiers) and OP tokens
    (checked for any non-ASCII) are inspected. STRING/COMMENT/f-string-text
    tokens are skipped — non-ASCII there is legitimate and never flagged.
    """
    import io
    import tokenize

    findings: list[str] = []
    seen: set[tuple[int, int, str]] = set()

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        # Un-tokenizable input is the syntax gate's job, not this detector's.
        return []

    for tok in tokens:
        if len(findings) >= _NONASCII_FEEDBACK_CAP:
            break
        line_no = tok.start[0]
        if tok.type == tokenize.NAME:
            if tok.string.isascii():
                continue
            scripts = _confusable_scripts_in_identifier(tok.string)
            if not scripts:
                continue
            for ch in tok.string:
                if ch.isascii() or _char_script(ch) not in _CONFUSABLE_SCRIPTS:
                    continue
                key = (line_no, tok.start[1], ch)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _describe_confusable_char(line_no, ch, identifier=tok.string)
                )
                if len(findings) >= _NONASCII_FEEDBACK_CAP:
                    break
        elif tok.type == tokenize.OP:
            for ch in tok.string:
                if ch.isascii():
                    continue
                key = (line_no, tok.start[1], ch)
                if key in seen:
                    continue
                seen.add(key)
                codepoint = f"U+{ord(ch):04X}"
                try:
                    name = unicodedata.name(ch)
                except ValueError:
                    name = None
                suffix = f", {name}" if name else ""
                findings.append(
                    f"line {line_no}: non-ASCII character {ch!r} ({codepoint}{suffix}) "
                    "in an operator/punctuation position"
                )
                if len(findings) >= _NONASCII_FEEDBACK_CAP:
                    break

    return findings[:_NONASCII_FEEDBACK_CAP]


def _confusable_code_error(relative_path: str, content: str, *, language: str) -> str | None:
    """Best-effort confusable-character check of one AI-produced payload.

    Returns a human/AI-readable error (naming the offending identifier(s)/char)
    or ``None`` when the payload is clean. Only Python files (by ``.py`` suffix,
    or ``language == "python"`` for an extensionless payload) are inspected;
    every other format is skipped — the syntax gate is the backstop. Cheap,
    deterministic, no LLM calls.
    """
    suffix = PurePosixPath(relative_path).suffix.lower()
    is_python = suffix == ".py" or (suffix == "" and language == "python")
    if not is_python:
        return None
    findings = _confusable_findings(content)
    if not findings:
        return None
    return "contains confusable non-ASCII character(s) in code position: " + "; ".join(findings)


def _confusable_check_enabled(config: dict[str, Any]) -> bool:
    """``implement.confusable_check`` — default ON (see defaults.yaml)."""
    section = config.get("implement")
    if isinstance(section, dict) and "confusable_check" in section:
        return bool(section["confusable_check"])
    return True


class Implementer:
    def __init__(
        self,
        project_root: Path,
        *,
        config: dict[str, Any] | None = None,
        ai_command: str | None = None,
        use_derived_steps: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config if config is not None else _load_project_config(self.project_root)
        self.ai_command = ai_command
        self.use_derived_steps = _use_derived_steps_enabled(self.config, use_derived_steps)
        # Injectable backoff between no-usable-output retries (tests stub it to
        # avoid real sleeps); mirrors ai_invoke._invoke_with_recovery's ``sleep``.
        self._sleep = sleep

    def run_implement(self, spec: ImplementSpec, *, feedback: str | None = None) -> ImplementationResult:
        """Read ``spec.design_node`` and generate files under ``spec.output_paths``.

        ``feedback`` carries review findings from a previous attempt (e.g. the
        verifiable-behavior coverage gate) and is injected into the prompt the
        same way ``codd generate`` injects review feedback.
        """
        spec = _normalize_spec_paths(spec)
        _check_guard_files_uniqueness(self.project_root, self.config)
        _create_output_paths(self.project_root, spec.output_paths)

        design_context = _load_design_context(self.project_root, self.config, spec.design_node)
        node_paths = build_document_node_path_map(self.project_root, self.config)
        node_paths[design_context.node_id] = design_context.path
        node_paths[design_context.path.as_posix()] = design_context.path

        explicit_dependencies = list(spec.dependency_design_nodes)
        design_dependencies = [entry["id"] for entry in design_context.depends_on if isinstance(entry.get("id"), str)]
        dependency_documents, document_conventions = _collect_dependency_documents(
            self.project_root,
            _ordered_unique([*explicit_dependencies, *design_dependencies]),
            node_paths,
            self.config,
        )
        design_document = DependencyDocument(
            node_id=design_context.node_id,
            path=design_context.path,
            content=design_context.content,
        )
        all_documents = _dedupe_documents([design_document, *dependency_documents])

        combined_conventions = _merge_conventions(
            _normalize_conventions(self.config.get("conventions", [])),
            design_context.conventions,
            document_conventions,
        )
        coding_principles = _load_coding_principles(self.project_root, self.config)
        capabilities = generator_module._resolve_generation_capabilities(
            self.config, self.project_root
        )
        design_md_content = (
            _load_design_md_content(self.project_root, capabilities=capabilities)
            if _spec_generates_ui_file(spec, (self.config.get("project") or {}).get("language"), design_context.content)
            else None
        )
        screen_flow_content = (
            _load_screen_flow_for_implementation(self.project_root, capabilities=capabilities)
            if _spec_looks_ui_facing(spec, design_context.content)
            else None
        )
        screen_flow_routes = (
            _select_screen_flow_routes_for_spec(spec, design_context.content, screen_flow_content)
            if screen_flow_content
            else []
        )
        impl_steps_context = (
            _implementation_steps_context(
                config=self.config,
                spec=spec,
                dependency_documents=all_documents,
                project_root=self.project_root,
            )
            if self.use_derived_steps
            else None
        )
        resolved_ai_command = generator_module._resolve_ai_command(
            self.config,
            self.ai_command,
            command_name="implement",
        )
        language = _normalize_implementation_language((self.config.get("project") or {}).get("language"))
        syntax_gate = _syntax_gate_enabled(self.config)
        confusable_check = _confusable_check_enabled(self.config)
        # Write-time syntax gate (implement.syntax_gate) + confusable-character
        # check (implement.confusable_check): either failure feeds BOUNDED
        # corrective retries through the existing feedback prompt injection
        # (default 3 total attempts, configurable via
        # implement.syntax_gate_max_attempts); with both off there is a single
        # attempt (legacy behavior).
        gate_active = syntax_gate or confusable_check
        # Two INDEPENDENT bounded retry budgets compose ADDITIVELY (never
        # nested — nesting would multiply into 4x3 model calls). The syntax
        # gate re-issues only on ImplementSyntaxGateError (files produced but
        # deterministically invalid); the no-usable budget re-issues only on a
        # transient no-usable-output attempt (empty/unparseable/all-out-of-scope
        # /filtered-to-0). A no-usable retry does NOT consume syntax budget (no
        # file reached validation) and vice-versa; the last exhausted gate
        # decides the error. With the syntax gate OFF there are no syntax
        # retries (legacy single-attempt-per-output semantics for that branch).
        syntax_gate_max_attempts = _syntax_gate_max_attempts(self.config) if gate_active else 1
        no_usable_file_retries = _no_usable_file_retries(self.config)

        current_feedback = feedback
        no_usable_retries_used = 0
        syntax_rejections = 0
        while True:
            try:
                generated_files = self._run_implementation_generation_attempt(
                    spec=spec,
                    design_context=design_context,
                    dependency_documents=dependency_documents,
                    combined_conventions=combined_conventions,
                    coding_principles=coding_principles,
                    design_md_content=design_md_content,
                    screen_flow_content=screen_flow_content,
                    screen_flow_routes=screen_flow_routes,
                    impl_steps_context=impl_steps_context,
                    capabilities=capabilities,
                    resolved_ai_command=resolved_ai_command,
                    language=language,
                    syntax_gate=syntax_gate,
                    confusable_check=confusable_check,
                    current_feedback=current_feedback,
                )
            except NoUsableGeneratedFiles as exc:
                # A no-usable retry never bypasses the eventual hard fail: once
                # the budget is spent, raise the SAME zero-files error a no-retry
                # world would (no softer warning, no partial success, no fallback
                # artifact) — carrying the last no-usable reason as context.
                if no_usable_retries_used >= no_usable_file_retries:
                    raise _zero_generated_files_error(spec) from exc
                no_usable_retries_used += 1
                print(
                    f"[codd] implement produced no usable files "
                    f"(no-usable retry {no_usable_retries_used}/{no_usable_file_retries}): "
                    f"{str(exc)[:200]}",
                    file=sys.stderr,
                )
                self._sleep(
                    DEFAULT_NO_USABLE_FILE_BACKOFF_SECONDS * no_usable_retries_used
                )
                continue
            except ImplementSyntaxGateError as exc:
                syntax_rejections += 1
                if syntax_rejections >= syntax_gate_max_attempts:
                    raise _syntax_gate_exhausted_error(
                        spec, exc, attempts=syntax_gate_max_attempts
                    ) from exc
                # Feedback only RESTATES the file-output contract (the rejected
                # files + the ASCII directive); it never broadens allowed paths,
                # changes the target language, or suggests placeholder files.
                current_feedback = _combine_feedback(feedback, exc.feedback_message())
                continue
            break

        return ImplementationResult(
            design_node=spec.design_node,
            output_paths=[_resolve_output_path(self.project_root, item) for item in spec.output_paths],
            generated_files=generated_files,
        )

    def _run_implementation_generation_attempt(
        self,
        *,
        spec: ImplementSpec,
        design_context: DesignContext,
        dependency_documents: list[DependencyDocument],
        combined_conventions: list[str],
        coding_principles: Any,
        design_md_content: str | None,
        screen_flow_content: str | None,
        screen_flow_routes: list[Any],
        impl_steps_context: Any,
        capabilities: ProjectCapabilities,
        resolved_ai_command: str,
        language: str,
        syntax_gate: bool,
        confusable_check: bool,
        current_feedback: str | None,
    ) -> list[Path]:
        """One prompt -> invoke -> write pass; the file-output contract boundary.

        Returns the generated files on success. Routes the three transient
        zero-file conditions into :class:`NoUsableGeneratedFiles` for the
        supervisor loop to retry:

        - (a) empty AI output / file-writing-agent "no (readable) file changes"
          — UNLESS ``skip_generation: true`` (then return ``[]``, the sole
          sanctioned 0-file success), checked BEFORE treating empty as
          no-usable;
        - (b) ``_write_generated_files`` ValueError (all blocks invalid / out of
          scope);
        - (c) the pass returned but filtered down to zero usable files.

        :class:`ImplementSyntaxGateError` propagates unchanged (the syntax/
        confusable gate stays active on EVERY retry). Path/scope validation is
        re-run verbatim — out-of-scope/absolute/traversal/non-output-prefix
        files stay rejected on retry; the prompt is never mutated to coax a file
        through.
        """
        skip_generation = _skip_generation_enabled(design_context.content)
        prompt = _build_implementation_prompt(
            config=self.config,
            design_context=design_context,
            spec=spec,
            dependency_documents=dependency_documents,
            conventions=combined_conventions,
            coding_principles=coding_principles,
            design_md_content=design_md_content,
            screen_flow_content=screen_flow_content,
            screen_flow_routes=screen_flow_routes,
            impl_steps_context=impl_steps_context,
            feedback=current_feedback,
            capabilities=capabilities,
        )
        prompt = generator_module._inject_lexicon(prompt, self.project_root)
        try:
            raw_output = generator_module._invoke_ai_command(
                resolved_ai_command,
                prompt,
                project_root=self.project_root,
            )
        except ValueError as exc:
            # skip_generation is the ONLY 0-file success: check it BEFORE
            # treating empty output as no-usable, so a skip doc never triggers a
            # retry. Empty output AND file-writing-agent "no (readable) file
            # changes" are both the no-usable class for a non-skip design.
            if not _is_no_usable_output_error(exc):
                raise
            if skip_generation:
                raw_output = ""
            else:
                raise NoUsableGeneratedFiles(str(exc)) from exc

        if skip_generation and not raw_output.strip():
            return []
        try:
            generated_files = _write_generated_files(
                project_root=self.project_root,
                design_context=design_context,
                spec=spec,
                dependency_documents=dependency_documents,
                language=language,
                raw_output=raw_output,
                syntax_gate=syntax_gate,
                confusable_check=confusable_check,
                root_artifact_patterns=_root_artifact_patterns_from_config(self.config),
            )
        except ImplementSyntaxGateError:
            # Files were produced but failed deterministic validation: this is
            # the syntax gate's domain, NOT a no-usable retry. Propagate.
            raise
        except ValueError as exc:
            # (b) parsed but ALL blocks invalid / out of scope. Retry as
            # no-usable instead of converting straight to the hard fail.
            raise NoUsableGeneratedFiles(str(exc)) from exc

        # (c) parsed but filtered to zero usable files (and not an explicit
        # skip): retry as no-usable.
        if len(generated_files) == 0 and not skip_generation:
            raise NoUsableGeneratedFiles(
                f"Design '{spec.design_node}' produced 0 usable generated files."
            )
        return generated_files


def implement_tasks(
    project_root: Path,
    *,
    design: str | None = None,
    output_paths: list[str] | tuple[str, ...] | None = None,
    dependency_design_nodes: list[str] | tuple[str, ...] | None = None,
    ai_command: str | None = None,
    clean: bool = False,
    use_derived_steps: bool | None = None,
    task: str | None = None,
    language: str | None = None,
    feedback: str | None = None,
    **_ignored: Any,
) -> list[ImplementationResult]:
    project_root = Path(project_root).resolve()
    config = _load_project_config(project_root)
    if language:
        # Per-invocation language override (Issue #20, v-kato): mismatched
        # codd init --language doesn't force a full re-init; spec authors can
        # ship an implement run with --language typescript and the project's
        # codd.yaml stays untouched.
        project_cfg = dict(config.get("project") or {})
        project_cfg["language"] = language
        config = {**config, "project": project_cfg}
    design_node = design or task
    if not design_node:
        raise ValueError("--design is required")
    outputs = list(output_paths or _default_output_paths_for_design(config, design_node))
    spec = ImplementSpec(
        design_node=design_node,
        output_paths=outputs,
        dependency_design_nodes=list(dependency_design_nodes or ()),
    )
    spec = _normalize_spec_paths(spec)
    if clean:
        _clean_output_paths(project_root, spec.output_paths)
    result = Implementer(
        project_root,
        config=config,
        ai_command=ai_command,
        use_derived_steps=use_derived_steps,
    ).run_implement(spec, feedback=feedback)
    return [result]


def get_valid_task_slugs(project_root: Path) -> set[str]:
    config = _load_project_config(Path(project_root).resolve())
    values: set[str] = set()
    for paths in _configured_output_path_groups(config).values():
        for item in paths:
            name = PurePosixPath(item).name
            if name:
                values.add(name)
    return values


def list_implement_tasks(project_root: Path) -> list[dict[str, Any]]:
    """Deterministically enumerate ALL implement tasks.

    Generalizes :func:`auto_detect_task` from "fail when multiple candidates
    exist" to "list every candidate", using the same two sources in the same
    precedence order:

    1. Configured implement targets — ``implement.default_output_paths`` /
       ``implement.implement_targets`` in codd.yaml, in declaration order.
    2. Approved derived tasks from ``.codd/derived_tasks`` (cache-path order),
       only consulted when no targets are configured.

    Each entry is ``{"task_id", "design_node", "source", "expected_outputs",
    "test_kinds"}`` where ``source`` is ``"configured"`` or ``"derived"``. For
    derived tasks ``design_node`` is the task's source design document (the
    artifact ``codd implement`` reads) and ``expected_outputs``/``test_kinds``
    are the task's declared intent (verbatim from the ``DerivedTask``), so
    callers can verify the implementer produced the intended *kind* of artifact.
    Configured targets declare no V-model intent, so those two fields are empty.
    """
    project_root = Path(project_root).resolve()
    config = _load_project_config(project_root)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for design in _configured_output_path_groups(config):
        if design in seen:
            continue
        seen.add(design)
        entries.append(
            {
                "task_id": design,
                "design_node": design,
                "source": "configured",
                "expected_outputs": [],
                "test_kinds": [],
            }
        )
    if entries:
        return entries

    from codd.llm.plan_deriver import iter_derived_task_records

    for _cache_path, record in iter_derived_task_records(project_root):
        for task in record.tasks:
            if not task.approved or task.id in seen:
                continue
            seen.add(task.id)
            entries.append(
                {
                    "task_id": task.id,
                    "design_node": task.source_design_doc or task.id,
                    "source": "derived",
                    "expected_outputs": list(task.expected_outputs),
                    "test_kinds": list(task.test_kinds),
                }
            )
    return entries


def auto_detect_task(project_root: Path) -> str:
    project_root = Path(project_root).resolve()
    config = _load_project_config(project_root)
    configured = _configured_output_path_groups(config)
    if len(configured) == 1:
        return next(iter(configured.keys()))

    candidates = _auto_detect_approved_derived_task_candidates(project_root)
    candidates = _ordered_unique(candidates)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("could not auto-detect a design node; pass --design")
    raise ValueError(
        "multiple implementation task candidates found "
        f"({', '.join(candidates)}); pass --design"
    )


def _auto_detect_approved_derived_task_candidates(project_root: Path) -> list[str]:
    from codd.llm.plan_deriver import iter_derived_task_records

    records = iter_derived_task_records(project_root)
    records.sort(key=lambda item: _path_mtime(item[0]), reverse=True)
    for _cache_path, record in records:
        approved = [task.id for task in record.tasks if task.approved]
        if approved:
            return approved
    return []


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _default_output_paths_for_design(config: dict[str, Any], design: str) -> list[str]:
    configured = _configured_output_path_groups(config)
    if design in configured:
        return configured[design]
    raise ValueError("--output is required unless codd.yaml maps the design node to output paths")


def _configured_output_path_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    implement = config.get("implement") if isinstance(config.get("implement"), dict) else {}
    groups: dict[str, list[str]] = {}
    for key in ("default_output_paths", "implement_targets"):
        payload = implement.get(key) if isinstance(implement, dict) else None
        if not isinstance(payload, dict):
            continue
        for design, paths in payload.items():
            if isinstance(paths, str):
                groups[str(design)] = [paths]
            elif isinstance(paths, list):
                groups[str(design)] = [str(item) for item in paths if str(item).strip()]
    return groups


def _normalize_spec_paths(spec: ImplementSpec) -> ImplementSpec:
    output_paths = [_normalize_project_path(item) for item in spec.output_paths]
    return ImplementSpec(
        design_node=spec.design_node,
        output_paths=output_paths,
        dependency_design_nodes=spec.dependency_design_nodes,
    )


def _normalize_project_path(path_text: str) -> str:
    text = str(path_text).strip()
    path = PurePosixPath(text)
    if path.is_absolute():
        return path.as_posix()
    if any(part == ".." for part in path.parts):
        raise ValueError(f"output path must stay within the project: {path_text}")
    if not path.parts:
        # PurePosixPath(".") and PurePosixPath("") both have empty .parts. "." is the
        # valid project root (root-module layouts e.g. Go declare repo-root outputs);
        # an empty string is not a path.
        if text in (".", "./"):
            return "."
        raise ValueError(f"output path must stay within the project: {path_text}")
    return path.as_posix().rstrip("/")


def _resolve_output_path(project_root: Path, output_path: str) -> Path:
    path = Path(output_path)
    resolved = path if path.is_absolute() else project_root / path
    return resolved.resolve(strict=False)


def _create_output_paths(project_root: Path, output_paths: list[str]) -> None:
    for output_path in output_paths:
        destination = _resolve_output_path(project_root, output_path)
        _ensure_inside_project(project_root, destination, "output path")
        destination.mkdir(parents=True, exist_ok=True)


def _clean_output_paths(project_root: Path, output_paths: list[str]) -> None:
    root = project_root.resolve(strict=False)
    for output_path in output_paths:
        destination = _resolve_output_path(project_root, output_path)
        _ensure_inside_project(project_root, destination, "output path")
        if destination.resolve(strict=False) == root:
            # NEVER delete the project root itself. A root-module layout (e.g. Go)
            # legitimately declares "." as an output root; cleaning it would wipe the
            # whole project (go.mod, .codd session, everything).
            continue
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()


def _ensure_inside_project(project_root: Path, path: Path, label: str) -> None:
    root = project_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay within the project: {path}") from exc


def _load_design_context(project_root: Path, config: dict[str, Any], design_node: str) -> DesignContext:
    path = _resolve_design_path(project_root, config, design_node)
    codd = _extract_frontmatter(path) or {}
    content = path.read_text(encoding="utf-8")
    return DesignContext(
        node_id=str(codd.get("node_id") or _relative_path(project_root, path).as_posix()),
        path=_relative_path(project_root, path),
        content=content,
        depends_on=generator_module._normalize_dependencies(codd.get("depends_on", [])),
        conventions=_normalize_conventions(codd.get("conventions", [])),
    )


def _resolve_design_path(project_root: Path, config: dict[str, Any], design_node: str) -> Path:
    """Resolve a ``source_design_doc`` reference to its canonical file path.

    Uses the shared deterministic reference resolver (ACG axis-1). Recovery of a
    unique-basename reference (e.g. ``docs/api_interface_contract.md`` →
    ``docs/design/api_interface_contract.md``) is allowed and audited; ambiguous
    / unresolved / wrong-subcategory references honest-fail. The
    ``FileNotFoundError`` contract is preserved (the resolver raises a
    ``FileNotFoundError`` subclass).
    """
    from codd.reference_resolution import (
        ReferenceResolutionError,
        record_reference_resolution_event,
        record_resolution_failure,
        resolve_document_ref,
    )
    from codd.scanner import build_document_reference_index

    index = build_document_reference_index(project_root, config)
    try:
        binding = resolve_document_ref(
            design_node,
            project_root=project_root,
            index=index,
            producer=None,
            ref_kind="source_design_doc",
            allow_recovery=True,
        )
    except ReferenceResolutionError as exc:
        record_resolution_failure(
            project_root,
            str(design_node),
            stage="implement_read",
            ref_kind="source_design_doc",
            producer=None,
            error=exc,
        )
        raise FileNotFoundError(f"design document not found: {design_node}") from exc

    record_reference_resolution_event(
        project_root,
        binding,
        stage="implement_read",
        status="recovered" if binding.recovered else "exact",
    )
    if binding.canonical_path is None:  # pragma: no cover - documents always have a path
        raise FileNotFoundError(f"design document not found: {design_node}")
    return project_root / binding.canonical_path


def _relative_path(project_root: Path, path: Path) -> Path:
    return path.resolve(strict=False).relative_to(project_root.resolve(strict=False))


def _dedupe_documents(documents: list[DependencyDocument]) -> list[DependencyDocument]:
    seen: set[str] = set()
    result: list[DependencyDocument] = []
    for document in documents:
        key = document.path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        result.append(document)
    return result


def _use_derived_steps_enabled(config: dict[str, Any], override: bool | None) -> bool:
    if override is not None:
        return bool(override)
    implementer_config = config.get("implementer")
    if isinstance(implementer_config, dict) and "use_derived_steps" in implementer_config:
        return bool(implementer_config.get("use_derived_steps"))
    return False


def _implementation_steps_context(
    *,
    config: dict[str, Any],
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    project_root: Path,
) -> str | None:
    from codd.llm.impl_step_deriver import render_impl_steps_for_prompt

    steps = _load_or_derive_implementation_steps(config, spec, dependency_documents, project_root)
    explicit = _filter_layer1_impl_steps([step for step in steps if not step.inferred], config)
    implicit = _filter_layer2_impl_steps([step for step in steps if step.inferred], config)
    if not explicit and not implicit:
        return None

    lines: list[str] = []
    if explicit:
        lines.extend(
            [
                "[Layer 1 - Explicit, from design]",
                render_impl_steps_for_prompt(explicit),
            ]
        )
    if implicit:
        if lines:
            lines.append("")
        lines.extend(
            [
                "[Layer 2 - Inferred, best-practice augment]",
                render_impl_steps_for_prompt(implicit),
            ]
        )
    return "\n".join(lines)


def _load_or_derive_implementation_steps(
    config: dict[str, Any],
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    project_root: Path,
) -> list[Any]:
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    from codd.llm.best_practice_augmenter import SubprocessAiCommandBestPracticeAugmenter
    from codd.llm.impl_step_deriver import (
        ImplStepCacheRecord,
        SubprocessAiCommandImplStepDeriver,
        impl_step_cache_path,
        merge_impl_steps,
        read_impl_step_cache,
        utc_timestamp,
        write_impl_step_cache,
    )

    context = {"project_root": project_root, "config": config, "project_context": {"project": config.get("project", {})}}
    cache_path = impl_step_cache_path(spec, context)
    record = read_impl_step_cache(cache_path)
    steps = list(record.steps) if record is not None else []
    explicit = [step for step in steps if not step.inferred]
    implicit = [step for step in steps if step.inferred]
    nodes = _dependency_documents_as_nodes(dependency_documents)

    derive_command = _ai_command_from_config(config, "impl_step_derive")
    if not explicit and derive_command and nodes:
        deriver = SubprocessAiCommandImplStepDeriver(
            SubprocessAiCommand(command=derive_command, project_root=project_root, config=config),
        )
        explicit = deriver.derive_steps(spec, nodes, context)
        record = read_impl_step_cache(cache_path)
        steps = list(record.steps) if record is not None else explicit
    elif not explicit and not derive_command and nodes:
        # K-2 cmd_345: detect silent fail of operation_flow_hint injection
        from codd.llm.criteria_expander import warn_if_operation_flow_unused

        warn_if_operation_flow_unused(config, nodes)

    augment_command = _ai_command_from_config(config, "best_practice_augment")
    if explicit and not implicit and augment_command and _best_practice_augment_enabled(config):
        augmenter = SubprocessAiCommandBestPracticeAugmenter(
            SubprocessAiCommand(command=augment_command, project_root=project_root, config=config),
        )
        implicit = augmenter.suggest_implicit_steps(spec, nodes, explicit, context)
        if implicit:
            merged = merge_impl_steps(explicit, implicit)
            base_record = read_impl_step_cache(cache_path)
            write_impl_step_cache(
                cache_path,
                ImplStepCacheRecord(
                    provider_id=(base_record.provider_id if base_record else "subprocess_ai_command"),
                    cache_key=((base_record.cache_key if base_record else spec.task_id) + ":augmented"),
                    task_id=(base_record.task_id if base_record else spec.task_id),
                    design_doc_sha=(base_record.design_doc_sha if base_record else ""),
                    prompt_template_sha=(base_record.prompt_template_sha if base_record else ""),
                    generated_at=utc_timestamp(),
                    design_docs=(base_record.design_docs if base_record else [node.path or node.id for node in nodes]),
                    steps=merged,
                ),
            )
            steps = merged

    return steps


def _dependency_documents_as_nodes(dependency_documents: list[DependencyDocument]):
    from codd.dag import Node

    return [
        Node(
            id=document.node_id,
            kind="design_doc",
            path=document.path.as_posix(),
            attributes={"content": document.content},
        )
        for document in dependency_documents
    ]


def _filter_layer1_impl_steps(steps: list[Any], config: dict[str, Any]) -> list[Any]:
    implementer_config = config.get("implementer") if isinstance(config.get("implementer"), dict) else {}
    per_kind = implementer_config.get("approval_mode_per_step_kind") if isinstance(implementer_config, dict) else {}
    if not isinstance(per_kind, dict):
        per_kind = {}
    approved: list[Any] = []
    for step in steps:
        mode = str(per_kind.get(step.kind, "required"))
        if mode == "auto" or bool(getattr(step, "approved", False)):
            approved.append(step)
    return approved


def _filter_layer2_impl_steps(steps: list[Any], config: dict[str, Any]) -> list[Any]:
    from codd.llm.approval import filter_layer_2_impl_steps

    return filter_layer_2_impl_steps(steps, config)


def _ai_command_from_config(config: dict[str, Any], name: str) -> str | None:
    ai_commands = config.get("ai_commands")
    if not isinstance(ai_commands, dict):
        return None
    value = ai_commands.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("command"), str):
        return value["command"]
    return None


def _best_practice_augment_enabled(config: dict[str, Any]) -> bool:
    implementer_config = config.get("implementer")
    if not isinstance(implementer_config, dict):
        return True
    return bool(implementer_config.get("use_best_practice_augmenter", True))


def _load_coding_principles(project_root: Path, config: dict[str, Any]) -> str | None:
    raw_path = config.get("coding_principles")
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("coding_principles must be a non-empty project-relative path when configured")

    principles_path = project_root / raw_path
    if not principles_path.exists():
        raise FileNotFoundError(f"coding_principles file not found: {raw_path}")

    return principles_path.read_text(encoding="utf-8")


def _load_design_md_content(
    project_root: Path, capabilities: ProjectCapabilities | None = None
) -> str | None:
    # Only UI projects have design tokens / a DESIGN.md to warn about. For a
    # non-UI project (CLI, library, service), silently skip — never emit a
    # "UI file generation" warning that does not apply.
    has_ui = capabilities is None or capabilities.user_interface
    try:
        from codd.design_md import DesignMdExtractor
    except ImportError:
        return None

    design_md_path = project_root / "DESIGN.md"
    if not design_md_path.exists():
        if has_ui:
            warnings.warn(
                "DESIGN.md not found. UI file generation will proceed without design tokens. "
                "Consider creating DESIGN.md (https://github.com/google-labs-code/design.md)",
                UserWarning,
                stacklevel=3,
            )
        return None

    result = DesignMdExtractor().extract(design_md_path)
    if result.error:
        warnings.warn(f"DESIGN.md parse error: {result.error}", UserWarning, stacklevel=3)
        return None

    lines = ["# DESIGN.md tokens (W3C Design Tokens spec)"]
    for token in result.tokens:
        lines.append(f"- {token.id} ({token.category}): {token.value}")
    return "\n".join(lines)


def _load_screen_flow_for_implementation(
    project_root: Path, capabilities: ProjectCapabilities | None = None
) -> str | None:
    # screen-flow.md / route definitions only apply to UI projects. A non-UI
    # project must never see a "UI file generation"/"route definitions" warning.
    has_ui = capabilities is None or capabilities.user_interface
    try:
        from codd.screen_flow_validator import find_screen_flow_path

        screen_flow_path = find_screen_flow_path(project_root)
    except ImportError:
        default_path = project_root / "docs" / "extracted" / "screen-flow.md"
        screen_flow_path = default_path if default_path.exists() else None

    if screen_flow_path is None:
        if has_ui:
            warnings.warn(
                "screen-flow.md not found. UI file generation will proceed without "
                "route definitions. Consider creating docs/extracted/screen-flow.md.",
                UserWarning,
                stacklevel=3,
            )
        return None
    return screen_flow_path.read_text(encoding="utf-8")


def _is_ui_task(task_title: str, task_description: str = "") -> bool:
    text = f"{task_title} {task_description}".casefold()
    for keyword in _UI_TASK_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", text):
            return True
    return False


def _is_wrapper_task(task_title: str, task_description: str = "") -> bool:
    text = f"{task_title} {task_description}".casefold()
    for keyword in _WRAPPER_TASK_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", text):
            return True
    return _is_ui_task(task_title, task_description)


def _check_guard_files_uniqueness(project_root: Path, config: dict[str, Any] | None = None) -> None:
    guard_files = list(_DEFAULT_GUARD_FILES)
    if config:
        implementer_config = config.get("implementer") or {}
        override = (
            implementer_config.get("guard_files")
            if isinstance(implementer_config, dict)
            else None
        )
        if isinstance(override, str) and override.strip():
            guard_files = [override.strip()]
        elif isinstance(override, list):
            configured = [str(item).strip() for item in override if str(item).strip()]
            if configured:
                guard_files = configured

    for filename in guard_files:
        candidates = sorted(project_root.rglob(filename))
        if len(candidates) > 1:
            warnings.warn(
                f"Multiple '{filename}' detected: {[str(p.relative_to(project_root)) for p in candidates]}. "
                f"Keep only ONE (usually the root-level file). "
                f"Remove duplicates to avoid dead code. "
                f"Override this check via codd.yaml [implementer] guard_files.",
                UserWarning,
                stacklevel=3,
            )


def _select_screen_flow_routes_for_spec(
    spec: ImplementSpec,
    design_content: str,
    screen_flow_content: str | None,
) -> list[str]:
    if not screen_flow_content:
        return []

    routes = _parse_screen_flow_routes_from_text(screen_flow_content)
    if not routes:
        return []

    relevant = [route for route in routes if _route_matches_spec(route, spec, design_content)]
    return relevant or routes[:20]


def _parse_screen_flow_routes_from_text(screen_flow_content: str) -> list[str]:
    routes: list[str] = []
    for match in _ROUTE_TOKEN_RE.finditer(screen_flow_content):
        route = _normalize_screen_flow_route(match.group(0))
        if route and route not in routes:
            routes.append(route)
    return routes


def _normalize_screen_flow_route(route: str) -> str:
    normalized = route.strip().strip("`\"'")
    normalized = normalized.rstrip(".,;。、)")
    if not normalized.startswith("/") or normalized.startswith("//"):
        return ""
    return normalized.rstrip("/") or "/"


def _route_matches_spec(route: str, spec: ImplementSpec, design_content: str) -> bool:
    task_text = " ".join(
        [
            spec.design_node,
            " ".join(spec.output_paths),
            " ".join(spec.dependency_design_nodes),
            design_content,
        ]
    ).casefold()
    if route.casefold() in task_text:
        return True

    if route == "/":
        return any(keyword in task_text for keyword in _ROUTE_HOME_KEYWORDS)

    segments = [_normalize_route_segment(segment) for segment in route.split("/") if segment]
    return any(segment and segment in task_text for segment in segments)


def _normalize_route_segment(segment: str) -> str:
    return _ROUTE_NORMALIZE_RE.sub("", segment.casefold())


def _skip_generation_enabled(design_content: str) -> bool:
    return bool(_SKIP_GENERATION_RE.search(design_content))


def _zero_generated_files_error(spec: ImplementSpec) -> Exception:
    from codd.cli import CoddCLIError

    return CoddCLIError(
        f"Design '{spec.design_node}' produced 0 generated files. "
        "If this is intentional, add 'skip_generation: true' to the design document. "
        "Otherwise, verify the design document contains sufficient implementation details."
    )


def _syntax_gate_exhausted_error(
    spec: ImplementSpec, error: ImplementSyntaxGateError, *, attempts: int
) -> Exception:
    """An honest failure beats a silent broken write."""
    from codd.cli import CoddCLIError

    failed_files = ", ".join(path for path, _ in error.all_failures)
    return CoddCLIError(
        f"Design '{spec.design_node}' produced invalid file(s) "
        f"after {attempts} attempt(s): {failed_files}. {error}. "
        "Nothing was written to disk for this task. "
        "Set 'implement.syntax_gate: false' in codd.yaml to opt out of the write-time syntax gate "
        "(or 'implement.confusable_check: false' to opt out of the confusable-character check only)."
    )


def _combine_feedback(original: str | None, addition: str) -> str:
    if original and original.strip():
        return f"{original.rstrip()}\n\n{addition}"
    return addition


def _spec_generates_ui_file(spec: ImplementSpec, language: Any, design_content: str) -> bool:
    for path in _candidate_generated_paths(spec, language, design_content):
        if path.suffix.lower() in UI_FILE_EXTENSIONS:
            return True
    return False


def _candidate_generated_paths(spec: ImplementSpec, language: Any, design_content: str) -> list[PurePosixPath]:
    candidates: list[PurePosixPath] = []
    fields = [
        spec.design_node,
        " ".join(spec.output_paths),
        " ".join(spec.dependency_design_nodes),
        design_content,
    ]
    for field in fields:
        for match in re.findall(r"[\w@./-]+\.(?:tsx|jsx|vue|svelte|swift|kt|dart)\b", field or "", re.IGNORECASE):
            candidates.append(PurePosixPath(match))

    default_extension = _default_generated_extension(language)
    for output_path in spec.output_paths:
        candidates.append(PurePosixPath(output_path) / f"index{default_extension}")

    normalized_language = _normalize_implementation_language(language)
    if normalized_language in {"typescript", "javascript"} and _spec_looks_ui_facing(spec, design_content):
        extensions = _implementation_language_extensions(normalized_language)
        if len(extensions) > 1:
            for output_path in spec.output_paths:
                candidates.append(PurePosixPath(output_path) / f"index{extensions[1]}")

    return candidates


def _spec_looks_ui_facing(spec: ImplementSpec, design_content: str) -> bool:
    return _is_ui_task(
        spec.design_node,
        " ".join([*spec.output_paths, *spec.dependency_design_nodes, design_content]),
    )


def _collect_dependency_documents(
    project_root: Path,
    initial_node_ids: list[str],
    node_paths: dict[str, Path],
    config: dict[str, Any] | None = None,
) -> tuple[list[DependencyDocument], list[dict[str, Any]]]:
    documents: list[DependencyDocument] = []
    conventions: list[dict[str, Any]] = []
    queue: deque[str] = deque(node_id for node_id in initial_node_ids if node_id)
    required_node_ids = set(initial_node_ids)
    seen: set[str] = set()
    missing: list[str] = []

    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)

        rel_path = node_paths.get(node_id)
        resolved_node_id = node_id
        if rel_path is None:
            try:
                context = _load_design_context(project_root, config or {}, node_id)
            except (FileNotFoundError, ValueError):
                if node_id in required_node_ids:
                    missing.append(node_id)
                continue
            rel_path = context.path
            resolved_node_id = context.node_id
            node_paths[context.node_id] = context.path
            node_paths[context.path.as_posix()] = context.path

        doc_path = project_root / rel_path
        if not doc_path.exists():
            if node_id in required_node_ids:
                raise ValueError(
                    f"dependency document {node_id!r} maps to {rel_path.as_posix()}, but the file does not exist"
                )
            continue

        content = doc_path.read_text(encoding="utf-8")
        documents.append(DependencyDocument(node_id=resolved_node_id, path=rel_path, content=content))

        codd = _extract_frontmatter(doc_path) or {}
        conventions.extend(_normalize_conventions(codd.get("conventions", [])))
        for dependency in generator_module._normalize_dependencies(codd.get("depends_on", [])):
            if dependency["id"] not in seen:
                queue.append(dependency["id"])

    if missing:
        raise ValueError(f"unable to resolve dependency document paths for: {', '.join(sorted(set(missing)))}")

    documents.sort(key=lambda document: document.path.as_posix())
    return documents, conventions


def _merge_conventions(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for convention in group:
            normalized = {
                "targets": [target for target in convention.get("targets", []) if isinstance(target, str)],
                "reason": str(convention.get("reason") or "").strip(),
            }
            key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged


def _build_implementation_prompt(
    *,
    config: dict[str, Any],
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    coding_principles: str | None,
    prior_task_outputs: list[dict[str, Any]] | None = None,
    design_md_content: str | None = None,
    screen_flow_content: str | None = None,
    screen_flow_routes: list[str] | None = None,
    impl_steps_context: str | None = None,
    feedback: str | None = None,
    capabilities: ProjectCapabilities | None = None,
) -> str:
    # Default to UI-capable so untyped/legacy (web-ish) projects keep emitting the
    # UI wrapper guidance exactly as before; only an explicit no-UI capability set
    # suppresses UI-specific wrapper vocabulary.
    has_ui = capabilities is None or capabilities.user_interface
    # E2E modality drives the e2e-contract (no-runtime-import) test rule below.
    # Default to the conservative ``cli`` baseline (``ProjectCapabilities``'s own
    # default) so an untyped project still gets the no-runtime-import guidance,
    # while an explicit browser/device modality (which legitimately imports a
    # client/runtime in its e2e layer) does NOT.
    e2e_modality = "cli" if capabilities is None else capabilities.e2e_modality
    project = config.get("project") or {}
    frameworks = project.get("frameworks") or []
    language = _normalize_implementation_language(project.get("language"))
    language_name = LANGUAGE_DISPLAY_NAMES.get(language, language)
    preferred_extensions = _implementation_language_extensions(language)
    default_extension = _default_generated_extension(language)
    code_fence_language = LANGUAGE_CODE_FENCE_MAP.get(language, language)
    framework_text = ", ".join(str(item) for item in frameworks) if frameworks else "(unspecified)"
    if frameworks:
        framework_guidance = f"- Honor the configured framework stack ({framework_text}) when relevant."
    else:
        framework_guidance = f"- Use idiomatic {language_name} patterns for the target project."

    if language in {"typescript", "javascript"} and len(preferred_extensions) > 1:
        jsx_extension = preferred_extensions[1]
        extension_guidance = (
            f"- If the work needs JSX-style UI components, emit {jsx_extension} files. "
            f"Otherwise prefer {default_extension} files."
        )
    elif len(preferred_extensions) > 1:
        extension_guidance = (
            f"- Use {default_extension} files by default. "
            f"Additional allowed extensions for this language family: {', '.join(preferred_extensions)}."
        )
    else:
        extension_guidance = f"- Use {default_extension} files for generated source unless the design explicitly requires another file type."

    prior_task_outputs = prior_task_outputs or []
    output_text = ", ".join(spec.output_paths)
    example_output = spec.output_paths[0]
    lines = [
        "You are generating implementation code from CoDD design documents.",
        f"Project name: {project.get('name') or '(unknown)'}",
        f"Primary language: {language}",
        f"Framework stack: {framework_text}",
        f"Design node: {design_context.path.as_posix()} ({design_context.node_id})",
        f"Requested design: {spec.design_node}",
        f"Output paths: {output_text}",
        "",
        "Mandatory instructions:",
        f"- Generate concrete production-oriented {language_name} source files.",
        framework_guidance,
        "- Reflect security, data boundaries, authentication, authorization, and auditability explicitly where the design requires them.",
        "- Treat behavioral contracts in the design as release-blocking: if one artifact produces, stores, emits, returns, restores, consumes, reflects, or derives a value/state, wire every side of that chain in executable code rather than leaving a stub, fixture, in-memory placeholder, static label, or unused response field.",
        "- Preserve trigger fidelity: automatic triggers, thresholds, timers, callbacks, stream events, retries, and cross-actor reflections must be implemented at the actor-facing/public boundary described by the design. A manual shortcut or direct lower-layer call is not equivalent unless the design explicitly declares that lower layer as the public surface.",
        "- For percentages, counts, durations, scores, thresholds, and latest/last values, implement the measurement source, durable persistence or derivation rule, readback path, consumer usage, and boundary behavior.",
        "- For UI files or user-facing strings, write production user copy only; never surface design rationale, test/demo/sample labels, implementation assumptions, TODOs, internal process, or environment notes as visible text.",
        "- The tool will prepend traceability comments to each generated file; do not emit separate metadata files.",
        "- Do not emit prose, explanations, Markdown headings, YAML, TODOs, placeholders, or file descriptions outside the required FILE blocks.",
        "- Every generated file path must stay under one of the output paths shown above.",
        *_root_artifact_prompt_lines(config),
        extension_guidance,
        "- Favor small coherent modules rather than one monolithic file.",
        "- Cross-file imports may use relative imports or project-local aliases, but keep the output internally coherent.",
        "",
    ]

    if _spec_targets_tests(spec, config):
        lines.extend(
            [
                "Verifiable-behavior traceability markers (release-blocking — the implement stage gate fails the build on any gap):",
                "- The design and its dependency test documents declare verifiable behaviors in a traceability table whose first column is `VB-<id>`. Reconcile EVERY declared VB id against the tests you write: for each VB, produce either a real test that PROVES it (carrying a `codd: covers vb=<id>` comment marker) or, if it genuinely cannot be tested yet, an explicit `codd: blocked vb=<id> reason=<short_reason>` marker. Never leave a declared VB silently uncovered.",
                "- A `codd: covers vb=<id>` marker is a CLAIM that the test proves that behavior. Only attach it to a test that (a) is actually executed — not `it.skip`/`test.todo`/`describe.skip`/`@pytest.mark.skip`/disabled — and (b) contains at least one assertion that would FAIL if the behavior were broken. A marker on an empty test, a skipped test, or a smoke test with no assertion is a false coverage claim and the authenticity gate will reject it. If no proving test exists, create or extend a real one FIRST, then mark it — do not add the marker to make the gate pass.",
                "- One marker per behavior, placed inside the test file as a line comment in the TARGET LANGUAGE's own comment syntax, immediately above the proving test; the marker text after the comment prefix is identical across languages. Example (pytest): `# codd: covers vb=VB-07` directly above `def test_rejects_missing_id(): ...` whose body asserts the rejection. Example (vitest): `// codd: covers vb=VB-07` directly above `it('rejects missing id', () => { expect(...).toThrow(); })`. Example (Go): `// codd: covers vb=VB-07` directly above `func TestRejectsMissingID(t *testing.T) { ... }` whose body calls `t.Fatal`/`t.Error` (or a testify assertion) when the behavior is broken. For any other language, use that language's line-comment prefix (`#`, `//`, `--`, …) with the same `codd: covers vb=<id>` text.",
                "",
                "Scoped assertions (precision, not weakening):",
                "- When a test asserts a CONTENT constraint over rendered or serialized output (e.g. \"displayed amounts contain no decimal point\", \"no personal data appears in the response\", \"every listed price shows a currency symbol\"), assert ONLY against the specific values or elements the constraint governs: parse and read the relevant nodes/fields under test (DOM elements, table cells, the parsed JSON values, the matched record), not the whole response body.",
                "- Do NOT enforce such a constraint with a regex, substring, or scan over the entire response/document. Full output carries standard scaffolding the constraint never meant to govern (markup metadata and attribute defaults, document preamble, framework boilerplate, asset/version query strings, generated ids) that can coincidentally match the pattern and FALSE-fail a correct implementation.",
                "- Still assert the constraint fully — keep proving every declared behavior — just bind each assertion to its subject under test rather than to incidental document scaffolding. Precise scoping makes the test stronger, not weaker.",
                "",
                "Test-helper import coherence (release-blocking — a missing helper symbol crashes the whole suite at collection):",
                "- Shared test utilities (fixtures, builders, assertion helpers, I/O wrappers) belong in ONE canonical location — a single helper module/package or the test `conftest` — and every test that needs them must import them from that same canonical location. Do not invent a different helper module per test or split the same helper across files.",
                "- That canonical location MUST actually define (or, if it is a package `__init__`, re-export) EVERY symbol the tests import from it. Never import a name that is not defined anywhere in the test tree. If a package `__init__` is the shared surface, its re-exports must match the symbols the tests import exactly — no missing names, no imports of names the helpers never provide.",
                "- Keep the import dialect for in-test-tree helpers consistent across the suite (one canonical form), so the helpers and their importers always agree.",
                "",
            ]
        )

        if e2e_modality == "cli":
            lines.extend(
                [
                    "E2E no-runtime-import contract (release-blocking — this project's e2e modality is CLI/subprocess):",
                    "- E2E tests AND their shared e2e helpers must drive the system the way a real user does: invoke the BUILT/INSTALLED entrypoint as a SUBPROCESS (run the command, then assert on its exit code, stdout, stderr, and the files/artifacts it produces). E2E tests and e2e helpers must NOT import the application/runtime (source) package — no `import <runtime_pkg>` and no `from <runtime_pkg> import ...`, not at module level and not inside a function/fixture body.",
                    "- An in-process helper that DOES import the runtime package (e.g. to call a function directly) is a UNIT/integration helper — put it in the unit test tree, NOT under the e2e helper package. Keep the e2e helper package purely subprocess-driven.",
                    "- If the design declares this e2e-no-runtime-import contract (e.g. an acceptance criterion / governance test asserting no runtime root is imported under the e2e tree), GENERATE that one governance check and then OBEY it everywhere in the e2e tree — do not generate the governance test and then violate it in a helper. Emit exactly ONE governance form per contract, and prefer an AST-based import check (parse the e2e files and inspect their import nodes) over a brittle literal-string/substring scan of the source text.",
                    "",
                ]
            )

    lines.extend([
        "Required output format (repeat this block for each file and output nothing else):",
        f"=== FILE: {example_output}/<filename>{default_extension} ===",
        f"```{code_fence_language}",
        "# code" if default_extension in {".py", ".rb"} else "// code",
        "```",
        "",
        "ABSOLUTE PROHIBITION: Outputting prose, planning notes, TODO markers, or files outside the requested output paths is a CRITICAL ERROR.",
        "",
        "Design document content:",
        design_context.content.rstrip(),
    ])

    if spec.dependency_design_nodes:
        lines.extend(["", "Explicit dependency design nodes:"])
        lines.extend(f"- {item}" for item in spec.dependency_design_nodes)

    if impl_steps_context:
        lines.extend(
            [
                "",
                "Implementation steps to follow (LLM-derived, project-approved):",
                impl_steps_context.rstrip(),
            ]
        )

    if coding_principles:
        lines.extend(
            [
                "",
                "Project coding principles (treat these as source-of-truth implementation rules):",
                coding_principles.rstrip(),
            ]
        )

    if conventions:
        lines.extend(
            [
                "",
                "Non-negotiable conventions:",
                "- These are release-blocking constraints. The code must embody them explicitly.",
                "- If a convention concerns security, data boundaries, or auth, implement a concrete control rather than only comments.",
            ]
        )
        for index, convention in enumerate(conventions, start=1):
            targets = ", ".join(target for target in convention.get("targets", []) if isinstance(target, str))
            reason = convention.get("reason") or "(no reason provided)"
            lines.append(f"{index}. Targets: {targets or '(no explicit targets)'}")
            lines.append(f"   Reason: {reason}")

    successful_prior_outputs = [s for s in prior_task_outputs if not s.get("error")]
    if successful_prior_outputs:
        lines.extend(
            [
                "",
                "Prior implementations:",
                "- The following summaries describe code that was already generated.",
                "- ABSOLUTE PROHIBITION: Re-implementing the same type definitions, utility functions, classes, guards, middleware, or helpers is a CRITICAL ERROR.",
                "- Reuse these implementations via imports. If a needed symbol already exists below, import it instead of redefining it.",
            ]
        )
        for summary in successful_prior_outputs:
            lines.extend(_format_prior_task_summary(summary))

    lines.extend(["", "Dependency documents:"])
    for document in dependency_documents:
        lines.extend(
            [
                f"--- BEGIN DEPENDENCY {document.path.as_posix()} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END DEPENDENCY {document.path.as_posix()} ---",
                "",
            ]
        )

    if design_md_content:
        lines.extend(
            [
                "DESIGN.md design token context:",
                "- Apply these W3C-style design tokens when generating UI files.",
                design_md_content.rstrip(),
                "",
            ]
        )

    if screen_flow_content:
        route_lines = list(screen_flow_routes or [])
        lines.extend(["--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---"])
        if route_lines:
            lines.append("This UI work must implement the relevant route(s):")
            for route in route_lines:
                lines.append(f"- {route}")
            lines.append("")
        lines.append(screen_flow_content[:SCREEN_FLOW_PROMPT_LIMIT].rstrip())
        lines.extend(["--- END SCREEN-FLOW ---", ""])

    if has_ui and _is_wrapper_task(spec.design_node, " ".join([*spec.output_paths, design_context.content])):
        lines.extend(
            [
                "--- WRAPPER COMPONENT RULES ---",
                "When generating a UI page wrapper that wraps a form, screen, or route component:",
                "1. Identify the component name from screen-flow.md or design docs. Do not rename it.",
                "2. Wire all callbacks the component requires.",
                "3. Pass required props from router, session, context, or equivalent platform services as needed.",
                "4. Do not generate a thin wrapper that ignores required component props.",
                "--- END WRAPPER RULES ---",
                "",
            ]
        )

    if feedback:
        lines.extend(
            [
                "",
                "--- REVIEW FEEDBACK (from previous implementation attempt) ---",
                "A reviewer found issues with a previous version of this implementation.",
                "You MUST address ALL of the following feedback in this generation:",
                feedback.rstrip(),
                "--- END REVIEW FEEDBACK ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _root_artifact_prompt_lines(config: dict[str, Any]) -> list[str]:
    """One prompt line declaring the repo-root infrastructure exception.

    Without it the AI obeys the confinement instruction and nests CI
    workflows/manifests inside the output paths, where they never function.
    Emitted only when the allowlist is active (non-empty patterns).
    """
    patterns = _root_artifact_patterns_from_config(config)
    if not patterns:
        return []
    return [
        "- EXCEPTION: repository-level infrastructure files matching "
        f"{', '.join(patterns)} belong at the REPOSITORY ROOT — emit them with "
        "their repo-root path (e.g. === FILE: .github/workflows/ci.yml ===), "
        "never nested inside the output paths.",
    ]


def _spec_targets_tests(spec: ImplementSpec, config: dict[str, Any]) -> bool:
    """Whether this implement run generates test artifacts (VB marker guidance)."""
    from codd.verifiable_behavior_audit import is_test_related_implement

    return is_test_related_implement(
        config, design_node=spec.design_node, output_paths=spec.output_paths
    )


def _write_generated_files(
    *,
    project_root: Path,
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    language: str,
    raw_output: str,
    syntax_gate: bool = True,
    confusable_check: bool = True,
    root_artifact_patterns: list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    file_payloads = _parse_file_payloads(
        raw_output, spec.output_paths, language, root_artifact_patterns=root_artifact_patterns
    )
    if syntax_gate or confusable_check:
        # Validate EVERY payload before writing ANY file: a gate failure leaves
        # the working tree untouched (no partial half-broken task output).
        failures: list[tuple[str, str]] = []
        rejected_payloads: dict[str, str] = {}
        confusable_failures: list[tuple[str, str]] = []
        for relative_path, content in file_payloads:
            error = _payload_syntax_error(relative_path, content) if syntax_gate else None
            if error is not None:
                failures.append((relative_path, error))
                rejected_payloads[relative_path] = content
                # A file that does not even parse is the syntax gate's domain;
                # the confusable detector (which tokenizes) would add noise.
                continue
            if confusable_check:
                confusable = _confusable_code_error(relative_path, content, language=language)
                if confusable is not None:
                    confusable_failures.append((relative_path, confusable))
        if failures or confusable_failures:
            raise ImplementSyntaxGateError(
                failures,
                payloads=rejected_payloads,
                confusable_failures=confusable_failures,
            )
    traceability_comment = _build_traceability_comment(design_context, spec, dependency_documents)
    output_prefixes = [PurePosixPath(item) for item in spec.output_paths]
    generated_paths: list[Path] = []
    for relative_path, content in file_payloads:
        destination = project_root / relative_path
        _ensure_inside_project(project_root, destination, "generated file")
        is_root_artifact = not any(
            _path_starts_with(PurePosixPath(relative_path), prefix) for prefix in output_prefixes
        )
        if is_root_artifact and _root_artifact_overwrite_blocked(destination, relative_path):
            import sys

            print(
                f"Warning: kept existing root file {relative_path} "
                "(no codd @generated-by marker; user-authored content wins)",
                file=sys.stderr,
            )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_prepend_traceability_comment(relative_path, traceability_comment, content), encoding="utf-8")
        generated_paths.append(destination)
    return generated_paths


def _root_artifact_patterns_from_config(config: dict[str, Any]) -> list[str]:
    """``implement.root_artifact_patterns`` — defaults shipped in defaults.yaml.

    An explicit project list replaces the defaults entirely (empty list = the
    allowlist is off and every out-of-scope path is dropped, the pre-FX2
    behavior).
    """
    section = config.get("implement")
    if isinstance(section, dict) and isinstance(section.get("root_artifact_patterns"), list):
        return [str(item).strip() for item in section["root_artifact_patterns"] if str(item).strip()]
    return list(DEFAULT_ROOT_ARTIFACT_PATTERNS)


def _root_artifact_overwrite_blocked(destination: Path, relative_path: str) -> bool:
    """User-authored repo-root files win over AI-emitted root artifacts.

    A root-destined payload may only overwrite a file the implementer itself
    generated (recognized by the ``@generated-by: codd implement`` traceability
    marker). Formats codd cannot mark (no comment prefix registered — YAML,
    JSON, TOML, suffix-less files) keep the implementer's normal overwrite
    semantics so repeated runs converge on the latest generation.
    """
    if not destination.exists():
        return False
    if COMMENT_PREFIX_BY_SUFFIX.get(PurePosixPath(relative_path).suffix.lower()) is None:
        return False
    try:
        existing = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    return "@generated-by: codd implement" not in existing


def _root_artifact_destination(
    path: PurePosixPath,
    output_prefixes: list[PurePosixPath],
    patterns: list[str] | tuple[str, ...],
) -> PurePosixPath | None:
    """Resolve the repo-root destination for a root-destined artifact path.

    Two accepted shapes (both deterministic, no LLM judgement):
      1. The AI emitted the repo-root path directly (``.github/workflows/ci.yml``).
      2. The AI obeyed the "stay under the output paths" instruction and nested
         the artifact under an output prefix (``src/.github/workflows/ci.yml``);
         stripping that prefix yields a pattern match, so the artifact is
         rerooted to where it actually functions.
    Returns ``None`` when the path is not a root artifact.
    """
    if not patterns:
        return None
    if _matches_root_artifact_pattern(path, patterns):
        return path
    for prefix in output_prefixes:
        if len(path.parts) > len(prefix.parts) and _path_starts_with(path, prefix):
            tail = PurePosixPath(*path.parts[len(prefix.parts):])
            if _matches_root_artifact_pattern(tail, patterns):
                return tail
    return None


def _matches_root_artifact_pattern(path: PurePosixPath, patterns: list[str] | tuple[str, ...]) -> bool:
    from fnmatch import fnmatchcase

    for pattern in patterns:
        text = str(pattern).strip()
        if not text:
            continue
        if text.endswith("/**"):
            prefix_parts = PurePosixPath(text[:-3]).parts
            if prefix_parts and len(path.parts) > len(prefix_parts) and tuple(path.parts[: len(prefix_parts)]) == prefix_parts:
                return True
            continue
        pattern_parts = PurePosixPath(text).parts
        if len(path.parts) == len(pattern_parts) and all(
            fnmatchcase(part, pattern_part)
            for part, pattern_part in zip(path.parts, pattern_parts)
        ):
            return True
    return False


def _reroot_bare_basename(
    path: PurePosixPath, output_prefixes: list[PurePosixPath]
) -> PurePosixPath | None:
    """Reroot a bare-basename file block under the single configured output dir.

    A text-out CLI (codex, and any other ``=== FILE: ===``-emitting model) names
    the file from the design's logical module (e.g. ``module:task_model`` →
    ``task_model.py``) and intermittently omits the configured source prefix —
    emitting ``task_model.py`` instead of ``src/task_model.py``. The block is
    well-formed and the content is real; dropping it as "outside output paths"
    discards genuine output and surfaces as the misleading "produced 0 generated
    files" error (the 2026-06-13 cross-CLI greenfield dogfood: codex emitted the
    correct files under bare names and every block was skipped).

    Conservative by construction so it captures only what the model genuinely
    meant for the output directory and never silently relocates a deliberately
    different path:

    * Only a **bare basename** (a single path component, e.g. ``task_model.py``)
      is rerooted. A multi-component path the model chose on purpose — a sibling
      tree (``src/other/service.py``), a different top-level dir (``tests/x.py``,
      ``lib/y.py``) — is NOT rerooted and stays dropped, preserving the
      out-of-scope skip semantics.
    * Only when exactly **one** output prefix is configured (the dominant
      implement-task case: a single source root such as ``src/``). With multiple
      output paths the target is ambiguous, so the bare name stays dropped.

    Returns the rerooted ``<prefix>/<basename>`` path, or ``None`` when no
    rerooting applies (caller then keeps the existing skip behavior). Fabricates
    nothing — it only relocates a path the model already emitted with content.
    """
    if len(output_prefixes) != 1:
        return None
    if len(path.parts) != 1:
        return None
    prefix = output_prefixes[0]
    if not prefix.parts:
        return None
    return prefix / path


def _parse_file_payloads(
    raw_output: str,
    output_paths: list[str],
    language: str,
    *,
    root_artifact_patterns: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    cleaned_output = raw_output.strip()
    output_prefixes = [PurePosixPath(item) for item in output_paths]
    root_patterns = list(root_artifact_patterns or ())
    matches = list(FILE_BLOCK_RE.finditer(cleaned_output))
    if not matches:
        fallback_content = _strip_code_fence(cleaned_output).strip()
        if not fallback_content:
            raise ValueError("AI command returned empty implementation output")
        extension = _default_generated_extension(language, fallback_content)
        return [(f"{output_paths[0]}/index{extension}", fallback_content.rstrip() + "\n")]

    payloads: list[tuple[str, str]] = []
    skipped: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned_output)
        block = cleaned_output[start:end].strip()
        path_text = match.group("path").strip()
        path = PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts:
            skipped.append(f"{path_text!r}: path traversal")
            continue
        root_destination = _root_artifact_destination(path, output_prefixes, root_patterns)
        if root_destination is not None:
            path = root_destination
        elif not any(_path_starts_with(path, prefix) for prefix in output_prefixes):
            rerooted = _reroot_bare_basename(path, output_prefixes)
            if rerooted is not None:
                path = rerooted
            else:
                skipped.append(f"{path_text!r}: outside output paths {output_paths!r}")
                continue

        content = _strip_code_fence(block, destination=path_text).strip()
        if not content:
            if _EMPTY_FENCE_BLOCK_RE.match(block):
                # The AI explicitly emitted an empty fenced block: the file is
                # intentionally empty (e.g. a package __init__.py). Write it
                # empty instead of skipping it or leaking literal fences.
                payloads.append((path.as_posix(), ""))
                continue
            skipped.append(f"{path_text!r}: empty content")
            continue
        payloads.append((path.as_posix(), content.rstrip() + "\n"))

    if skipped:
        import sys
        for reason in skipped:
            print(f"Warning: skipped generated file - {reason}", file=sys.stderr)

    if not payloads:
        raise ValueError(
            f"AI produced {len(matches)} file block(s) but all were invalid: {'; '.join(skipped)}"
        )

    return payloads


def _path_starts_with(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return tuple(path.parts[: len(prefix.parts)]) == prefix.parts


def _summarize_generated_task_output(
    project_root: Path,
    spec: ImplementSpec,
    generated_files: list[Path],
) -> dict[str, Any]:
    exported_types: list[str] = []
    exported_functions: list[str] = []
    exported_classes: list[str] = []
    exported_values: list[str] = []
    relative_files: list[str] = []

    for file_path in generated_files:
        relative_files.append(file_path.relative_to(project_root).as_posix())
        summary = _extract_export_summary(file_path.read_text(encoding="utf-8"))
        exported_types.extend(summary["exported_types"])
        exported_functions.extend(summary["exported_functions"])
        exported_classes.extend(summary["exported_classes"])
        exported_values.extend(summary["exported_values"])

    return {
        "task_id": spec.task_id,
        "task_title": spec.title,
        "directory": ", ".join(spec.output_paths),
        "files": relative_files,
        "exported_types": _ordered_unique(exported_types),
        "exported_functions": _ordered_unique(exported_functions),
        "exported_classes": _ordered_unique(exported_classes),
        "exported_values": _ordered_unique(exported_values),
    }


def _extract_export_summary(content: str) -> dict[str, list[str]]:
    summary = {
        "exported_types": [match.group("name") for match in EXPORT_TYPE_RE.finditer(content)],
        "exported_functions": [match.group("name") for match in EXPORT_FUNCTION_RE.finditer(content)],
        "exported_classes": [match.group("name") for match in EXPORT_CLASS_RE.finditer(content)],
        "exported_values": [match.group("name") for match in EXPORT_VALUE_RE.finditer(content)],
    }

    for match in EXPORT_NAMED_BLOCK_RE.finditer(content):
        body = match.group("body")
        block_is_type = bool(match.group("type_prefix"))
        for raw_item in body.split(","):
            item = raw_item.strip()
            if not item:
                continue
            item_is_type = block_is_type
            if item.startswith("type "):
                item_is_type = True
                item = item[5:].strip()
            exported_name = item.split(" as ")[-1].strip()
            if not exported_name:
                continue
            bucket = "exported_types" if item_is_type else "exported_values"
            summary[bucket].append(exported_name)

    return {key: _ordered_unique(values) for key, values in summary.items()}


def _format_prior_task_summary(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"- Task {summary.get('task_id') or '(unknown)'}: {summary.get('task_title') or '(untitled task)'}",
        f"  Directory: {summary.get('directory') or '(unknown directory)'}",
    ]

    files = [str(item) for item in summary.get("files", []) if str(item).strip()]
    if files:
        lines.append(f"  Files: {', '.join(files)}")

    for label, key in (
        ("Exported types", "exported_types"),
        ("Exported functions", "exported_functions"),
        ("Exported classes", "exported_classes"),
        ("Other exported values", "exported_values"),
    ):
        items = [str(item) for item in summary.get(key, []) if str(item).strip()]
        if items:
            lines.append(f"  {label}: {', '.join(items)}")

    return lines


def _build_traceability_comment(
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
) -> str:
    lines = [
        "@generated-by: codd implement",
        f"@generated-from: {design_context.path.as_posix()} ({design_context.node_id})",
        f"@design-node: {spec.design_node}",
        f"@output-paths: {', '.join(spec.output_paths)}",
    ]
    for document in dependency_documents:
        lines.append(f"@generated-from: {document.path.as_posix()} ({document.node_id})")
    return "\n".join(lines)


def _prepend_traceability_comment(relative_path: str, comment_block: str, content: str) -> str:
    prefix = COMMENT_PREFIX_BY_SUFFIX.get(PurePosixPath(relative_path).suffix.lower())
    if prefix is None:
        return content

    formatted_comment = "\n".join(f"{prefix} {line}" for line in comment_block.splitlines())
    marker = f"{prefix} @generated-by: codd implement"

    # A shebang (`#!...`) is only valid on the FIRST line of a file (e.g. a Node
    # bin entry `#!/usr/bin/env node`, a Python script, a shell script). If the
    # generated content opens with one, the provenance banner must go AFTER it so
    # the shebang stays on line 1 — otherwise tools like `tsc` reject it
    # (TS18026: '#!' can only be used at the start of a file). We only treat the
    # shebang as line 1 when it is *literally* first (no leading blank lines);
    # if the model emitted content before it, we cannot make it valid and fall
    # back to the default top-of-file banner.
    if content.startswith("#!"):
        newline_index = content.find("\n")
        if newline_index == -1:
            shebang_line, rest = content, ""
        else:
            shebang_line, rest = content[:newline_index], content[newline_index + 1 :]
        # Idempotency: if the banner already sits directly after the shebang,
        # leave the file untouched (re-running implement must not duplicate it).
        if rest.lstrip().startswith(marker):
            return content
        body = rest.lstrip()
        if body:
            return f"{shebang_line}\n{formatted_comment}\n\n{body}"
        return f"{shebang_line}\n{formatted_comment}\n"

    stripped_content = content.lstrip()
    if stripped_content.startswith(marker):
        return content
    return f"{formatted_comment}\n\n{stripped_content}"


def _strip_code_fence(block: str, *, destination: str | None = None) -> str:
    """Unwrap a markdown code fence around an AI-produced file payload.

    ``destination`` is the relative path the payload will be written to; it
    only gates the *orphan-line* cleanup below — content destined for markdown
    files (where fence lines are legitimate) is never orphan-stripped. A
    ``None`` destination is treated as non-markdown (the fallback payload path
    never targets markdown files).
    """
    stripped = block.strip()
    # An EMPTY fenced block (``` lang? NEWLINE ```), optionally with blank
    # lines between the fences, means "this file is intentionally empty".
    # The old wrapper regex required a body line, so the literal fences leaked
    # to disk verbatim (observed: __init__.py files written as ```python\n```,
    # an immediate SyntaxError).
    if _EMPTY_FENCE_BLOCK_RE.match(stripped):
        return ""
    # Non-greedy `.*?` captures up to the FIRST closing fence; any trailing
    # prose/markdown after the fence is discarded (Issue #22, v-kato).
    # Drop the `$` end-of-string anchor so the match still wins when the
    # LLM ignored the "no commentary" instruction and appended explanations.
    # `\s*\n` after the language tag tolerates trailing spaces and CRLF.
    fenced = re.match(r"^```(?:[a-zA-Z0-9_+-]+)?\s*\n(?P<body>.*?)\r?\n```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group("body")
    if destination is not None and PurePosixPath(destination).suffix.lower() in _MARKDOWN_SUFFIXES:
        return stripped
    return _strip_orphan_fence_lines(stripped)


_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_EMPTY_FENCE_BLOCK_RE = re.compile(r"^```[a-zA-Z0-9_+-]*[ \t]*(?:\r?\n\s*)?```$")
# A fence OPENER line (optional language tag) and a bare closing fence line.
_ORPHAN_OPEN_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*[ \t]*\r?$")
_ORPHAN_CLOSE_FENCE_RE = re.compile(r"^```[ \t]*\r?$")


def _strip_orphan_fence_lines(content: str) -> str:
    """Drop a LEADING orphan fence opener and/or a TRAILING lone ``` line.

    Covers half-wrapped payloads (opening fence without a closing one, or the
    reverse) that the full-wrapper regex cannot match. Only the first and last
    non-blank lines are considered — fence lines in the MIDDLE of the content
    (e.g. inside a string literal containing markdown) are never touched.
    """
    lines = content.splitlines(keepends=True)
    start, end = 0, len(lines)
    for index in range(len(lines)):
        if not lines[index].strip():
            continue
        if _ORPHAN_OPEN_FENCE_RE.match(lines[index].rstrip("\n")):
            start = index + 1
        break
    for index in range(len(lines) - 1, start - 1, -1):
        if not lines[index].strip():
            continue
        if _ORPHAN_CLOSE_FENCE_RE.match(lines[index].rstrip("\n")):
            end = index
        break
    if start == 0 and end == len(lines):
        return content
    return "".join(lines[start:end])


def _looks_like_tsx(content: str) -> bool:
    return bool(re.search(r"</?[A-Z][A-Za-z0-9]*|return\s*\(\s*<", content))


def _normalize_implementation_language(language: Any) -> str:
    normalized = str(language or "").strip().lower()
    if not normalized:
        return "typescript"
    return LANGUAGE_ALIASES.get(normalized, normalized)


def _implementation_language_extensions(language: Any) -> tuple[str, ...]:
    normalized = _normalize_implementation_language(language)
    return LANGUAGE_EXT_MAP.get(normalized, LANGUAGE_EXT_MAP["typescript"])


def _default_generated_extension(language: Any, content: str | None = None) -> str:
    normalized = _normalize_implementation_language(language)
    extensions = _implementation_language_extensions(normalized)
    if normalized in {"typescript", "javascript"} and len(extensions) > 1 and content and _looks_like_tsx(content):
        return extensions[1]
    return extensions[0]


def _slug_from_text(text: str) -> str:
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    ascii_text = re.sub(r"_+", "_", ascii_text)
    return ascii_text


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered
