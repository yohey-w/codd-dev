"""Regression test for Issue #22 (v-kato) — markdown leak via _strip_code_fence.

v2.14.0's `implementer._strip_code_fence` used the regex
    r"^```(?:[a-zA-Z0-9_+-]+)?\\s*\\n(?P<body>.*)\\n```$"
The trailing `$` anchored the match to end-of-string, so if an LLM ignored
the "no prose after code" instruction and appended explanatory markdown
after the closing fence, the WHOLE block (fence markers and trailing prose
included) was kept and written verbatim into the generated source file.
That immediately broke TypeScript / JavaScript typecheck (TS1127 etc.).

v2.18.0 drops the `$` anchor and switches the body capture to non-greedy
`.*?`, so the match wins on the FIRST closing fence and discards anything
after it. These tests pin that behaviour.
"""

from __future__ import annotations

from codd.implementer import _strip_code_fence


def test_strip_code_fence_drops_trailing_markdown_prose():
    """The exact failure pattern from Issue #22."""
    raw = (
        "```typescript\n"
        "const x: number = 1;\n"
        "export default x;\n"
        "```\n"
        "---\n"
        "**Implementation notes:**\n"
        "This code defines a single export.\n"
    )
    cleaned = _strip_code_fence(raw)
    assert "const x: number = 1;" in cleaned
    assert "export default x;" in cleaned
    # Markdown rot below the fence must be gone — no `---`, no `**`, no prose.
    assert "Implementation notes" not in cleaned
    assert "---" not in cleaned
    assert "**" not in cleaned
    # The closing fence itself must also be stripped.
    assert "```" not in cleaned


def test_strip_code_fence_drops_trailing_prose_without_horizontal_rule():
    """Some LLMs append prose without a separator — same trap."""
    raw = (
        "```ts\n"
        "export const greet = (name: string) => `Hello, ${name}`;\n"
        "```\n"
        "Note: this uses template literals.\n"
    )
    cleaned = _strip_code_fence(raw)
    assert "export const greet" in cleaned
    assert "Note: this uses template literals" not in cleaned


def test_strip_code_fence_keeps_pure_fenced_block_intact():
    """Backward compat: a clean fenced block (no trailing prose) is unchanged."""
    raw = (
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```"
    )
    cleaned = _strip_code_fence(raw)
    assert cleaned == "def hello():\n    return 'world'"


def test_strip_code_fence_no_fence_returns_input_stripped():
    """When the LLM forgets the fence entirely, the helper falls back to the
    raw (stripped) string — pin that legacy path still works."""
    raw = "const x: number = 1;\nexport default x;\n"
    cleaned = _strip_code_fence(raw)
    assert cleaned == raw.strip()


def test_strip_code_fence_supports_languages_with_plus_or_dash_in_fence():
    """Some LLMs annotate fences with hyphenated names (e.g. ```c++```)."""
    raw = "```c++\nint main() { return 0; }\n```\nThis is C++.\n"
    cleaned = _strip_code_fence(raw)
    assert "int main()" in cleaned
    assert "This is C++" not in cleaned
