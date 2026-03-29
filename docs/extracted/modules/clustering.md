---
codd:
  node_id: design:extract:clustering
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/clustering.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# clustering

> 1 files, 168 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `build_feature_clusters` | `codd/clustering.py:16` | `build_feature_clusters(facts: ProjectFacts) -> None` |






## Public API

- `build_feature_clusters`


## Test Coverage

**Coverage**: 1.0 (1 / 1)
Tests: tests/test_clustering.py



## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`
- `from codd.extractor import FeatureCluster`


## Files

- `codd/clustering.py`

## Tests

- `tests/test_clustering.py` — tests: test_resolve_callee_module_exact, test_resolve_callee_module_dotted, test_resolve_callee_module_unknown, test_connected_components, test_group_by_prefix, test_common_prefix, test_build_feature_clusters_by_calls, test_build_feature_clusters_by_prefix, test_build_feature_clusters_single_module