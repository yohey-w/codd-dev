---
codd:
  user_journeys:
    - name: login
      criticality: critical
      required_capabilities:
        - authenticate
      steps:
        - action: navigate
          target: /login
        - action: assert
          value: logged_in
  coverage_axes:
    # A cross-browser NFR axis with one critical variant. No test in this
    # fixture mentions the (browser, safari) pair, so C9 must report a missing
    # test for that variant — the construction-derived gap in gold.yaml.
    - axis_type: browser
      rationale: Login must work across supported browsers.
      variants:
        - id: safari
          criticality: critical
---

# App design

The browser/safari coverage variant has no exercising test — an intentional NFR
coverage gap.
