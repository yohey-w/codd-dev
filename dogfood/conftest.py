# The dogfood/ tree is CoDD's self-verification harness, not part of the
# gated test suite. Its fixtures/ are synthetic mini-repos used as INPUT DATA by
# the axis runners (e.g. run_d11_zoo.py); files like fixtures/*/test_app.py are
# fixture content, not suite tests, and must never be collected by a bare
# `pytest` run from the repo root. (CI runs `pytest tests/`, which already
# excludes this tree; this guard protects the bare-root invocation too.)
collect_ignore_glob = ["fixtures/*", "fixtures/**/*"]
