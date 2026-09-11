r"""ANSI escape-sequence stripping for captured tool output.

CoDD's verify/repair layers read the *text* a test runner prints — "collected N
items", "N passed", ``FAILED tests/...``, ``src/calc.py:2: in add`` — and turn it
into verdicts and repair targets. Every one of those readers is a regex over the
captured stdout/stderr of a spawned process.

Terminal colouring silently defeats all of them. A runner that decides to emit
colour writes ``\x1b[32m1 passed\x1b[0m`` instead of ``1 passed``: the ``\b\d+``
word boundary no longer matches (``m1`` is one word), and a ``^`` MULTILINE anchor
no longer sits at the start of the visible text because the escape prefixes it.

This is not hypothetical, and it is not a "your terminal is weird" problem:

* ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` make pytest, jest, vitest and tsc colour
  their output **even when stdout is a pipe**. Claude Code — the harness CoDD
  ships a plugin for — exports ``FORCE_COLOR=3`` into every command it runs, so
  a ``codd verify`` invoked from inside it reads colour-contaminated output.
* A user's ``pytest.ini``/``addopts`` may carry ``--color=yes``.
* Wrapper scripts (``script -q``, CI pseudo-TTY runners) allocate a PTY.

The failure modes cut both ways, which is why sanitising is *anti-false-green*
work rather than cosmetics:

* **False RED** — the positive-execution evidence check cannot see ``1 passed``
  in ``\x1b[32m1 passed\x1b[0m`` and hard-fails a genuinely green run; the repair
  attributor cannot see the ``src/calc.py`` frame and reports ``failed_nodes=[]``
  with ``failure_class='unknown'``, so auto-repair has nothing to engage.
* **False GREEN** — the zero-collected guard looks for the literal substring
  ``"collected 0 items"``, which a colour code inserted mid-phrase hides, letting
  a run that executed nothing pass.

So: strip once, at the boundary where captured output is handed to a parser.
Stripping is idempotent and safe to apply twice.
"""

from __future__ import annotations

import re

#: CSI sequences (``ESC [ … final-byte``) — SGR colour/style is the common case —
#: plus the two-character escapes (e.g. ``ESC ( B``) some runners emit. Matches
#: the ECMA-48 shape: parameter bytes ``0x30-0x3F``, intermediate ``0x20-0x2F``,
#: final ``0x40-0x7E``.
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: OSC sequences (``ESC ] … BEL`` or ``ESC ] … ESC \``) — hyperlinks and window
#: titles. pytest-sugar and some CI reporters emit these.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

#: Remaining single-character escapes (``ESC`` + one byte in ``0x40-0x5F``), e.g.
#: ``ESC ( B`` charset selection. Applied last so CSI/OSC win first.
_SHORT_ESC_RE = re.compile(r"\x1b[@-Z\\-_()#]")


def strip_ansi(text: str | None) -> str:
    """Return ``text`` with ANSI escape sequences removed.

    ``None`` becomes ``""`` so call sites can drop their ``or ""`` guards.
    Idempotent: stripping already-clean text is a no-op, so it is safe to apply
    both at the capture boundary and defensively inside a parser.
    """
    if not text:
        return ""
    if "\x1b" not in text:
        # Fast path: the overwhelmingly common uncoloured case pays nothing.
        return text
    cleaned = _OSC_RE.sub("", text)
    cleaned = _CSI_RE.sub("", cleaned)
    return _SHORT_ESC_RE.sub("", cleaned)
