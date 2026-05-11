"""Unit tests for codd.fix.interactive_prompt."""

from __future__ import annotations

import io

import pytest

from codd.fix.interactive_prompt import (
    InteractivePrompt,
    Option,
    PromptAbort,
)


def _prompt(input_text: str = "", *, non_interactive: bool = False,
            on_ambiguity: str = "abort"):
    stdin = io.StringIO(input_text)
    stdout = io.StringIO()
    p = InteractivePrompt(
        non_interactive=non_interactive,
        on_ambiguity=on_ambiguity,
        stdin=stdin,
        stdout=stdout,
    )
    return p, stdout


def test_choice_returns_selected_option():
    p, _ = _prompt("2\n")
    options = [Option("a", "A"), Option("b", "B"), Option("c", "C")]
    assert p.choice("pick", options) == "b"


def test_choice_abort_raises():
    p, _ = _prompt("abort\n")
    options = [Option("a", "A")]
    with pytest.raises(PromptAbort):
        p.choice("pick", options)


def test_choice_invalid_then_valid():
    p, out = _prompt("99\nfoo\n1\n")
    options = [Option("a", "A"), Option("b", "B")]
    assert p.choice("pick", options) == "a"
    assert "Invalid selection" in out.getvalue()


def test_choice_all_option():
    p, _ = _prompt("all\n")
    options = [Option("a", "A"), Option("b", "B")]
    assert p.choice("pick", options, allow_all=True) == "__all__"


def test_text_returns_stripped_value():
    p, _ = _prompt("  hello world  \n")
    assert p.text("describe") == "hello world"


def test_text_empty_raises_when_disallowed():
    p, _ = _prompt("\n")
    with pytest.raises(PromptAbort):
        p.text("describe")


def test_text_empty_allowed():
    p, _ = _prompt("\n")
    assert p.text("describe", allow_empty=True) == ""


def test_confirm_yes_no():
    p, _ = _prompt("y\n")
    assert p.confirm("ok?") is True
    p, _ = _prompt("n\n")
    assert p.confirm("ok?") is False


def test_confirm_default_on_empty():
    p, _ = _prompt("\n")
    assert p.confirm("ok?", default=True) is True


def test_non_interactive_choice_top1():
    p, _ = _prompt("", non_interactive=True, on_ambiguity="top1")
    options = [Option("a", "A"), Option("b", "B")]
    assert p.choice("pick", options) == "a"


def test_non_interactive_choice_default():
    p, _ = _prompt("", non_interactive=True, on_ambiguity="default")
    options = [Option("a", "A"), Option("b", "B", is_default=True)]
    assert p.choice("pick", options) == "b"


def test_non_interactive_choice_abort():
    p, _ = _prompt("", non_interactive=True, on_ambiguity="abort")
    options = [Option("a", "A")]
    with pytest.raises(PromptAbort):
        p.choice("pick", options)


def test_non_interactive_confirm_requires_default():
    p, _ = _prompt("", non_interactive=True)
    with pytest.raises(PromptAbort):
        p.confirm("ok?")
    p, _ = _prompt("", non_interactive=True)
    assert p.confirm("ok?", default=True) is True


def test_show_diff_accept():
    p, _ = _prompt("y\n")
    assert p.show_diff("- foo\n+ bar\n", question="apply?") == "accept"


def test_show_diff_reject():
    p, _ = _prompt("n\n")
    assert p.show_diff("- foo\n+ bar\n", question="apply?") == "reject"


def test_invalid_on_ambiguity_raises():
    with pytest.raises(ValueError):
        InteractivePrompt(on_ambiguity="bogus")
