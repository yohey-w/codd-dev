---
codd: {}
---

# App design

This fixture declares a forbidden_evidence pattern in codd.yaml. The source file
src/leak.ts intentionally contains a string that matches it, so the
negative_space check must report a hit.
