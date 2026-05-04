"""Tests for codd drift URL comparison."""

from __future__ import annotations

from codd.drift import DriftEntry, DriftResult, _find_closest, compute_drift


def test_compute_drift_no_drift():
    result = compute_drift(["/admin", "/users"], ["/admin", "/users"])

    assert isinstance(result, DriftResult)
    assert result.design_urls == ["/admin", "/users"]
    assert result.impl_urls == ["/admin", "/users"]
    assert result.drift == []
    assert result.exit_code == 0


def test_compute_drift_design_only():
    result = compute_drift(
        ["/admin/dashboard", "/tenant/users", "/my"],
        ["/central-admin", "/tenant-admin/users", "/learner"],
    )

    design_only = [entry for entry in result.drift if entry.kind == "design-only"]
    assert [entry.url for entry in design_only] == ["/admin/dashboard", "/tenant/users", "/my"]
    assert all(isinstance(entry, DriftEntry) for entry in result.drift)
    assert result.exit_code == 1


def test_compute_drift_impl_only():
    result = compute_drift([], ["/api/health", "/api/v1/enrollments"])

    assert [entry.kind for entry in result.drift] == ["impl-only", "impl-only"]
    assert [entry.url for entry in result.drift] == ["/api/health", "/api/v1/enrollments"]
    assert [entry.source for entry in result.drift] == ["implementation", "implementation"]
    assert result.exit_code == 1


def test_compute_drift_mixed():
    result = compute_drift(["/admin", "/old-path"], ["/admin", "/new-path"])

    assert [(entry.kind, entry.url) for entry in result.drift] == [
        ("design-only", "/old-path"),
        ("impl-only", "/new-path"),
    ]
    assert result.exit_code == 1


def test_compute_drift_closest_match():
    result = compute_drift(["/admin/dashboard"], ["/central-admin", "/admin/users"])

    assert result.drift[0].kind == "design-only"
    assert result.drift[0].url == "/admin/dashboard"
    assert result.drift[0].closest_match == "/admin/users"


def test_find_closest_empty_candidates():
    assert _find_closest("/admin", []) == ""
