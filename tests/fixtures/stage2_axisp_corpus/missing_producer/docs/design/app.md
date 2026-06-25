---
codd:
  user_journeys:
    - name: checkout
      criticality: critical
      required_capabilities:
        - read_cart
      steps:
        - action: navigate
          target: /cart
        - action: assert
          value: cart_loaded
  resource_contracts:
    # cart_data is consumed (required) by a capability on the critical journey,
    # but NO producer / externally_provided_by is declared for it. That is the
    # construction-derived missing_producer gap recorded in gold.yaml.
    - resource: cart_data
      consumers:
        - capability: read_cart
          required: true
---

# App design

The checkout journey needs cart_data, but nothing in the contract graph produces
it — an intentional missing-producer gap.
