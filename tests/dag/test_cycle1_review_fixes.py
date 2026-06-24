"""Red-before-green tests for cycle-1 review findings (Sonnet + Opus confirmed).

- Finding #3 (false-green): cardinality `_signal_asserted` substring-matched the
  whole sentinel entry, so a member_signal that is a substring of the sentinel
  prefix ("source", "blob") always registered as asserted.
- Finding #5 (read-only path traversal): stale_evidence `_resolve_source` returned
  any absolute path without a project-root jail.
"""

from codd.dag.checks.cardinality_coverage import (
    _SOURCE_TEXT_BLOB_PREFIX,
    _signal_asserted,
)
from codd.dag.checks.stale_evidence import _resolve_source


def test_signal_asserted_ignores_sentinel_prefix_collision():
    # The source blob is present, but the signal "source" appears ONLY inside the
    # sentinel prefix, never in the actual test content → must NOT count as asserted.
    blob = _SOURCE_TEXT_BLOB_PREFIX + "\nassert order_total == expected\n"
    assert _signal_asserted("source", {blob}) is False
    assert _signal_asserted("blob", {blob}) is False
    # A signal genuinely present in the content still registers.
    assert _signal_asserted("order_total", {blob}) is True
    # Exact-membership (explicit attr) path is unaffected.
    assert _signal_asserted("explicit_sig", {"explicit_sig"}) is True


def test_resolve_source_jails_absolute_path_outside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    inside = root / "in.txt"
    inside.write_text("y")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    # Absolute path outside the project root → refused (None), never hashed.
    assert _resolve_source(root, str(outside)) is None
    # Absolute path inside the root → resolved and returned.
    assert _resolve_source(root, str(inside)) == inside.resolve()
    # Relative path → joined under root.
    assert _resolve_source(root, "in.txt") == inside.resolve()
