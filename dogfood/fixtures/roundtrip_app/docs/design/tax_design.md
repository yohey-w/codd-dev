---
codd:
  node_id: "design:tax"
  type: design
  status: approved
  confidence: 0.9
  depends_on:
    - id: "req:billing-requirements"
      relation: implements
---

# Tax Design

## 1. Overview

Tax is expressed in basis points (1% = 100 bps) and added to a monetary amount
in cents. The computation truncates toward zero (integer arithmetic).
