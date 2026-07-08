r"""Ratchet lint: keep hardcoded ``language`` dispatch out of shared core.

Fable5-designed increment (see
``dogfood/fable5_reply_2026-07-08_quota-prioritization.md``, item 4; widened in
``dogfood/fable5_reply_2026-07-08_review2.md``, SECONDARY #4). Converts the
manual per-diff ``grep`` discipline (which dies with the campaign session) into a
PERMANENT machine gate that ratchets the number of hardcoded
``language ==`` / ``language !=`` / ``language in (...)`` branches in shared core
*downward-only*.

Mechanism
---------
* Walk ``codd/**/*.py`` EXCLUDING ``codd/languages/**`` and ``codd/stack/**`` — those
  are the legitimate language-zone dirs where per-language dispatch is allowed.
* Strip prose FIRST: languages are legitimately *mentioned* in comments and strings
  (docstrings routinely quote ``language == "python"`` verbatim as an example of the
  anti-pattern being avoided). We tokenize each file and blank string CONTENTS +
  drop COMMENTs before matching, so only real *code* branches are counted. A real
  branch ``self.language == "python"`` survives as ``self.language == ""`` (the
  opening quote and ``==`` remain, so the regex still matches); a docstring or
  comment mention collapses to ``""`` / nothing and is not matched.
* Match the ``language``-dispatch regexes (Fable5's spec, widened in review #2 after
  a proven live blind spot) against the code-only text:
    ``\blanguage\s*[!=]=\s*["']``                                  (``==`` AND ``!=``)
    ``\blanguage\s+in\s*[({\[]``                                   (positive membership)
    ``\blanguage\s+not\s+in\s*[({\[]``                             (negated membership)
    ``\blanguage\s*\.\s*(?:lower|casefold)\s*\(\s*\)\s*==\s*["']`` (normalize-then-compare)
    ``\bmatch\s+language\s*:``                                     (structural match)
  The original ``==``-only pattern let ``treesitter.py:124`` ``self.language != "java"``
  — real dispatch in a PINNED file — sail through green; the ``[!=]=`` widen closes
  exactly that hole (treesitter re-derived 10 -> 11 by this change).
* Count matches PER FILE and compare to the pinned snapshot ``PINNED``.

The ratchet tightens in BOTH directions:

* current > pinned, or a NEW file with matches not in ``PINNED``  -> FAIL: a hardcode
  branch entered shared core; route it through a profile accessor or add the file to
  ``codd/languages`` / ``codd/stack``.
* current < pinned, or a pinned file now at 0 / removed           -> FAIL: prune the
  pin. A silent decrease is pin-rot / drift; the pin MUST be updated down when
  branches are removed (this is how the ratchet stays honest).
* exact match                                                     -> PASS.

NOT COVERED (the DoD step-2 manual grep remains mandatory)
----------------------------------------------------------
This gate is a ratchet on the DOMINANT ``language``-token dispatch shapes; it is NOT
proof that shared core is language-free. It is anchored on the literal token
``language`` and blanks all string CONTENTS by design, so the following escape forms
are STRUCTURALLY INVISIBLE to it and are caught ONLY by the mandatory step-2 grep +
human review in the Definition of Done:

* Aliased dispatch variables -- ``lang == "python"`` (the branch token is not
  ``language``, so no regex here anchors on it).
* Literal constants -- ``SUPPORTED = ("python", "java")`` followed by a membership
  test against that name (the language set is bound to a non-``language`` symbol).
* Dict-literal / ``.get(language)`` dispatch -- ``{...}[language]`` /
  ``registry.get(language)``. DELIBERATELY not widened: undecidable against the
  sanctioned registry pattern (``LANGUAGE_EXT_MAP[language]``), and string-blanking
  erases the discriminating keys.
* Helper functions whose language parameter is named something other than
  ``language`` -- e.g. ``def dispatch(lang): ...``.
* Yoda form -- ``"python" == language``. Zero live hits; documented here rather than
  added as a noisy reversed pattern.
* Tool / framework / runtime literals -- ``pip``, ``pytest``, ``venv``, ``__main__``,
  etc. are ENTIRELY OUT OF SCOPE: the gate anchors only on the token ``language`` and
  says nothing about non-language hardcodes.

Do NOT read a green result here as evidence of language-freeness: it means only that
no NEW ``language``-token dispatch of the covered shapes was added to shared core.

Dependency-free (stdlib ``pathlib`` / ``re`` / ``tokenize`` / ``io``), fast,
deterministic (sorted walk). If a file cannot be tokenized (e.g. a syntax error),
the lint falls back to matching that file's raw text rather than crashing.

Regenerating ``PINNED`` is intentional friction: any legitimate change to a pinned
count is a two-line diff here, reviewed alongside the code change that caused it.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

# Repo root = parent of tests/. Location-independent (no absolute paths pinned).
REPO_ROOT = Path(__file__).resolve().parent.parent
CODD_ROOT = REPO_ROOT / "codd"

# Legitimate language-zone dirs: per-language dispatch lives here by design.
EXCLUDED_PREFIXES = ("codd/languages/", "codd/stack/")

# The Fable5-spec regexes, applied to the CODE-ONLY reconstruction. Widened in
# review #2 after a LIVE blind spot: ``treesitter.py:124`` ``self.language != "java"``
# is real dispatch in a pinned file that the original ``==``-only pattern missed.
# See the "NOT COVERED" section in the module docstring for what these still cannot
# see by design.
PATTERNS = (
    # ``language == "x"`` AND ``language != "x"`` (``[!=]=`` covers both directions).
    re.compile(r'\blanguage\s*[!=]=\s*["\']'),
    # ``language in (...)`` positive membership dispatch.
    re.compile(r"\blanguage\s+in\s*[({\[]"),
    # ``language not in (...)`` negated membership dispatch.
    re.compile(r"\blanguage\s+not\s+in\s*[({\[]"),
    # ``language.lower() == "x"`` / ``language.casefold() == "x"`` normalize-then-compare.
    re.compile(r'\blanguage\s*\.\s*(?:lower|casefold)\s*\(\s*\)\s*==\s*["\']'),
    # ``match language:`` structural dispatch (not valid Python in any other context).
    re.compile(r"\bmatch\s+language\s*:"),
)

# f-string literal segments (Python 3.12+) can carry prose too; blank them like
# regular string contents. ``None`` on older interpreters where the type is absent.
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)

# Pinned snapshot of per-file real-branch counts, generated from the live repo.
# Only files with count > 0 are listed. Keys are POSIX relpaths from REPO_ROOT.
# The ratchet fails on ANY drift from this dict (up OR down).
PINNED = {
    "codd/parsing/_shared.py": 2,
    "codd/parsing/tests_builddeps.py": 6,
    "codd/parsing/treesitter.py": 11,
}


def _is_excluded(relpath: str) -> bool:
    return relpath.startswith(EXCLUDED_PREFIXES)


def _code_only_text(source: str) -> str:
    """Reconstruct source with comments dropped and string contents blanked.

    String tokens become ``""`` (delimiters kept, contents gone); f-string literal
    middles are blanked; comments vanish; every other token keeps its exact text.
    Layout is rebuilt from token positions so the two regexes still see real code
    structure (the surviving opening quote / bracket) but never prose. On any
    tokenizer error the raw source is returned unchanged (fail-safe).
    """
    out: list[str] = []
    last_row, last_col = 1, 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    for tok in tokens:
        srow, scol = tok.start
        erow, ecol = tok.end
        if srow > last_row:
            out.append("\n" * (srow - last_row))
            last_col = 0
        if scol > last_col:
            out.append(" " * (scol - last_col))
        if tok.type == tokenize.COMMENT:
            piece = ""
        elif tok.type == tokenize.STRING:
            piece = '""'
        elif _FSTRING_MIDDLE is not None and tok.type == _FSTRING_MIDDLE:
            piece = ""
        else:
            piece = tok.string
        out.append(piece)
        last_row, last_col = erow, ecol
    return "".join(out)


def _count_matches(text: str) -> int:
    return sum(len(pat.findall(text)) for pat in PATTERNS)


def scan_shared_core() -> dict[str, int]:
    """Return {relpath: count} for shared-core files with count > 0 (sorted walk)."""
    counts: dict[str, int] = {}
    for path in sorted(CODD_ROOT.rglob("*.py")):
        relpath = path.relative_to(REPO_ROOT).as_posix()
        if _is_excluded(relpath):
            continue
        code = _code_only_text(path.read_text(encoding="utf-8"))
        count = _count_matches(code)
        if count > 0:
            counts[relpath] = count
    return counts


def test_language_free_core_ratchet() -> None:
    current = scan_shared_core()

    increases: list[str] = []  # count rose, or a brand-new hardcode file appeared
    decreases: list[str] = []  # count fell, or a pinned file dropped to 0 / vanished

    for relpath in sorted(set(current) | set(PINNED)):
        pinned = PINNED.get(relpath, 0)
        now = current.get(relpath, 0)
        if now > pinned:
            increases.append(f"  {relpath}: {pinned} -> {now} (+{now - pinned})")
        elif now < pinned:
            decreases.append(f"  {relpath}: {pinned} -> {now} (-{pinned - now})")

    problems: list[str] = []
    if increases:
        problems.append(
            "A new `language ==` / `language !=` / `language in (...)` hardcode branch "
            "entered shared core. Route it through a profile accessor, or place the file "
            "under codd/languages/ or codd/stack/. Increased sites:\n"
            + "\n".join(increases)
        )
    if decreases:
        problems.append(
            "Hardcode branches were REMOVED but PINNED was not updated (pin-rot). The "
            "ratchet only tightens: prune the pin down to the new counts in "
            "tests/test_language_free_core_ratchet.py. Decreased sites:\n"
            + "\n".join(decreases)
        )

    assert not problems, "\n\n".join(problems)
