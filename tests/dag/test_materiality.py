"""Materiality overlay: a PASS over 0 checked items is a *vacuous* pass, not a
verified one. The overlay is generic (reads ``checked_count`` off any result),
so it carries no per-check / project / framework literal."""

from dataclasses import dataclass

from codd.dag.materiality import is_vacuous_pass, vacuous_pass_results


@dataclass
class _R:
    status: str = "pass"
    passed: bool = True
    skipped: bool = False
    checked_count: int | None = None
    check_name: str = "demo_check"


# 4-fixture (GPT cycle-1 design):
def test_zero_count_pass_is_vacuous():  # candidate
    assert is_vacuous_pass(_R(status="pass", checked_count=0)) is True


def test_verified_pass_is_not_vacuous():  # green control
    assert is_vacuous_pass(_R(status="pass", checked_count=3)) is False


def test_skip_is_not_vacuous():  # false-red guard (intended non-application)
    assert is_vacuous_pass(_R(status="skip", skipped=True, checked_count=0)) is False


def test_legacy_result_without_count_is_not_vacuous():  # backward-compat
    assert is_vacuous_pass(_R(status="pass", checked_count=None)) is False


def test_vacuous_pass_results_filters_only_vacuous():
    results = [
        _R(status="pass", checked_count=0, check_name="empty"),
        _R(status="pass", checked_count=2, check_name="real"),
        _R(status="skip", skipped=True, checked_count=0, check_name="skipped"),
    ]
    vacuous = vacuous_pass_results(results)
    assert [r.check_name for r in vacuous] == ["empty"]
