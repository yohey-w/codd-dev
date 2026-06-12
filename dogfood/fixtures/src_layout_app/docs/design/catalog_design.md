---
# Deliberately non-canonical "dialect": flat top-level codd keys (legacy style)
# instead of a nested codd: block, plus a plural type and list-form depends_on.
# The frontmatter layer must tolerate this without crashing.
node_id: "design:catalog"
type: designs
status: draft
confidence: 0.7
depends_on:
  - id: "req:catalog-requirements"
    relation: implements
codd_restoration:
  provenance:
    - statement: "Catalog stores items keyed by SKU."
      band: amber
      sources: [code]
  open_questions: []
---

# Catalog Design

## 1. Overview

The catalog keeps an in-memory map of SKU → Item and exposes add/get/total.
