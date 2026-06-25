---
codd:
  user_journeys:
    # The journey declares an expected outcome (purchase_done) and a plan task
    # produces it (see implementation_plan.md), but no e2e test verifies the
    # journey. That missing acceptance signal is the gap in gold.yaml.
    - name: purchase_flow
      criticality: critical
      required_capabilities:
        - checkout
      expected_outcome_refs:
        - lexicon:purchase_done
      steps:
        - action: navigate
          target: /checkout
        - action: expect_url
          value: /done
---

# App design

The purchase_flow journey has an expected outcome and a build task, but no e2e
acceptance test — an intentional acceptance-signal gap.
