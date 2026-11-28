"""Regressions for ownership after test runs and candidate failure matching."""

import textwrap

import pytest

from .test_implement_test_feedback import HELPER, TEST_FILE, feedback, project_factory


@pytest.mark.parametrize("side_effect_run", ["suite", "isolate"])
def test_helper_is_returned_after_test_replaces_it_with_symlink(project_factory, side_effect_run):
    subject = textwrap.dedent(f"""\
        from pathlib import Path
        import pytest
        from checks.support import helpers

        def replace_helper():
            helper = Path({HELPER!r})
            replacement = helper.with_name("replacement.py")
            replacement.write_text("def value():\\n    return 0\\n")
            helper.unlink()
            helper.symlink_to(replacement.name)
            assert helper.is_symlink()

        @pytest.fixture
        def cleanup(request):
            yield
            if len(request.session.items) == 1 and {side_effect_run == 'isolate'}:
                replace_helper()

        def test_subject(cleanup):
            assert helpers.value() == 7

        def test_z_cleanup():
            if {side_effect_run == 'suite'}:
                replace_helper()
        """)
    pipeline, root, _, _ = project_factory(
        rounds=2,
        initial={"case": {TEST_FILE: subject}, "helper": {HELPER: "def value():\n    return [7][1]\n"}},
        owned={"case": (TEST_FILE,), "helper": (HELPER,)},
        fixes={"helper": {"files": {HELPER: "def value():\n    return 7\n"}}},
    )

    result = pipeline.run(root)

    assert result.status == "success", result.error
    [record] = feedback(root)["rounds"]
    assert record["runs"][0]["failures_and_errors"] == 1
    assert [(a["owner"], a["decision"]) for a in record["attempts"]] == [("helper", "accepted")]


def test_candidate_isolation_counts_failure_even_if_teardown_replaces_helper(project_factory, monkeypatch):
    subject = textwrap.dedent(f"""\
        from pathlib import Path
        import pytest
        from checks.support import helpers

        @pytest.fixture
        def rotate_helper(request):
            yield
            if len(request.session.items) == 1 and helpers.EXTRA_FIXED:
                helper = Path({HELPER!r})
                replacement = helper.with_name("replacement.py")
                replacement.write_text("def value():\\n    return 0\\n")
                helper.unlink()
                helper.symlink_to(replacement.name)

        def test_a(rotate_helper):
            assert helpers.value() == 7

        def test_b(request):
            if len(request.session.items) > 1:
                assert helpers.extra() == 8
            elif not helpers.EXTRA_FIXED:
                request.addfinalizer(lambda: pytest.fail("isolation failed in teardown"))
        """)
    helper = textwrap.dedent("""\
        EXTRA_FIXED = False

        def value():
            return [7][1]

        def extra():
            return 8 if EXTRA_FIXED else [8][1]
        """)
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: subject}, "helper": {HELPER: helper}},
        owned={"case": (TEST_FILE,), "helper": (HELPER,)},
        fixes={"helper": {"files": {HELPER: helper.replace("False", "True")}}},
    )
    before_candidate = []
    reimplement = pipeline._reimplement_tasks

    def record_original(project_root, *args, **kwargs):
        before_candidate.append((project_root / HELPER).read_bytes())
        return reimplement(project_root, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_reimplement_tasks", record_original)

    result = pipeline.run(root)

    assert result.status == "success", result.error
    [record] = feedback(root)["rounds"]
    [attempt] = record["attempts"]
    [isolation] = [run for run in record["runs"] if run["purpose"] == "candidate isolate"]
    assert (attempt["suite_before"], attempt["suite_after"], attempt["isolated_before"]) == (2, 1, 1)
    assert isolation["case"] == f"{TEST_FILE}::test_a"
    assert isolation["failures_and_errors"] == 1
    assert attempt["isolated_after"] == 1
    assert attempt["decision"] == "rejected"
    assert [(root / HELPER).read_bytes()] == before_candidate


@pytest.mark.parametrize("changed_field", ["origin", "phase"])
def test_candidate_isolation_matches_the_returned_failure(project_factory, changed_field):
    alternate = "checks/support/alternate.py"
    isolated_failure = "alternate.fail()" if changed_field == "origin" else "request.addfinalizer(helpers.fail)"
    subject = textwrap.dedent(f"""\
        from checks.support import alternate, helpers

        def test_passing():
            assert callable(helpers.fail)

        def test_subject(request):
            if not helpers.FIXED:
                helpers.fail()
            elif len(request.session.items) == 1:
                {isolated_failure}
        """)
    helper = "FIXED = False\n\ndef fail():\n    assert False\n"
    pipeline, root, _, _ = project_factory(
        initial={
            "case": {TEST_FILE: subject},
            "helper": {HELPER: helper, alternate: "def fail():\n    assert False\n"},
        },
        owned={"case": (TEST_FILE,), "helper": ("checks/support/",)},
        fixes={"helper": {"files": {HELPER: helper.replace("FIXED = False", "FIXED = True")}}},
    )

    result = pipeline.run(root)

    assert result.status == "success", result.error
    [record] = feedback(root)["rounds"]
    [attempt] = record["attempts"]
    [isolation] = [run for run in record["runs"] if run["purpose"] == "candidate isolate"]
    assert attempt["isolated_before"] == 1
    assert isolation["case"] == f"{TEST_FILE}::test_subject"
    assert isolation["failures_and_errors"] == 1
    assert attempt["isolated_after"] == 0
    assert attempt["decision"] == "accepted"
