---
codd:
  node_id: "design:invoice"
  type: design
  status: approved
  confidence: 0.9
  depends_on:
    - id: "req:billing-requirements"
      relation: implements
---

# Invoice Design

## 1. Overview

An invoice owns a list of line items. Each line item has a description, a unit
price in cents, and a quantity; its subtotal is price × quantity. The invoice
total is the sum of subtotals. A discount is applied as a whole-percent
reduction of the total.
