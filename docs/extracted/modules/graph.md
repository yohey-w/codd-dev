---
codd:
  node_id: design:extract:graph
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/graph.py
---
# graph

> 1 files, 293 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `CEG` | `codd/graph.py:13` | — |
| function | `close` | `codd/graph.py:55` | `close(self)` |
| function | `upsert_node` | `codd/graph.py:78` | `upsert_node(self, node_id: str, node_type: str, path: str = None, name: str = None, module: str = None)` |
| function | `get_node` | `codd/graph.py:91` | `get_node(self, node_id: str) -> Optional[dict]` |
| function | `count_nodes` | `codd/graph.py:94` | `count_nodes(self) -> int` |
| function | `find_nodes_by_path` | `codd/graph.py:97` | `find_nodes_by_path(self, path: str) -> list` |
| function | `get_convention_edges` | `codd/graph.py:100` | `get_convention_edges(self, node_id: str) -> list` |
| function | `add_edge` | `codd/graph.py:112` | `add_edge(self, source_id: str, target_id: str, relation: str, semantic: str, confidence: float = 0.5, condition: str = None) -> int` |
| function | `get_outgoing_edges` | `codd/graph.py:133` | `get_outgoing_edges(self, node_id: str, min_confidence: float = 0.0) -> list` |
| function | `get_incoming_edges` | `codd/graph.py:144` | `get_incoming_edges(self, node_id: str, min_confidence: float = 0.0) -> list` |
| function | `count_edges` | `codd/graph.py:155` | `count_edges(self) -> int` |
| function | `add_evidence` | `codd/graph.py:160` | `add_evidence(self, edge_id: int, source_type: str, method: str, score: float, detail: str = None, is_negative: bool = False) -> int` |
| function | `propagate_impact` | `codd/graph.py:190` | `propagate_impact(self, start_node_id: str, max_depth: int = 10, min_confidence: float = 0.0) -> dict` |
| function | `classify_band` | `codd/graph.py:220` | `classify_band(self, confidence: float, evidence_count: int, green_threshold: float = 0.90, green_min_evidence: int = 2, amber_threshold: float = 0.50) -> str` |
| function | `purge_auto_generated` | `codd/graph.py:236` | `purge_auto_generated(self) -> dict` |
| function | `count_human_evidence` | `codd/graph.py:276` | `count_human_evidence(self) -> int` |
| function | `stats` | `codd/graph.py:286` | `stats(self) -> dict` |






## Public API

- `CEG`
- `close`
- `upsert_node`
- `get_node`
- `count_nodes`
- `find_nodes_by_path`
- `get_convention_edges`
- `add_edge`
- `get_outgoing_edges`
- `get_incoming_edges`
- `count_edges`
- `add_evidence`
- `propagate_impact`
- `classify_band`
- `purge_auto_generated`
- `count_human_evidence`
- `stats`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `CEG.propagate_impact` | `get_incoming_edges` | `codd/graph.py:209` | no |
| `CEG.stats` | `count_nodes` | `codd/graph.py:289` | no |
| `CEG.stats` | `count_edges` | `codd/graph.py:290` | no |
| `CEG.stats` | `count_human_evidence` | `codd/graph.py:292` | no |

## Test Coverage

**Coverage**: 0.65 (11 / 17)
Tests: tests/test_bridge.py, tests/test_graph.py

**Uncovered symbols**: `count_edges`, `count_human_evidence`, `count_nodes`, `find_nodes_by_path`, `get_convention_edges`, `purge_auto_generated`






## Files

- `codd/graph.py`

## Tests

- `tests/test_bridge.py` — tests: test_build_frontmatter_with_source_files, test_build_frontmatter_without_source_files, test_build_frontmatter_empty_source_files, test_build_frontmatter_preserves_other_fields, test_load_frontmatter_source_files_creates_file_nodes, test_load_frontmatter_source_files_creates_bridge_edges, test_load_frontmatter_bridge_edge_relation_is_extracted_from, test_load_frontmatter_no_source_files_creates_no_bridge_edges, test_load_frontmatter_multiple_source_files_multiple_edges, test_load_frontmatter_bridge_edge_confidence_is_0_85; fixtures: ceg- `tests/test_graph.py` — tests: test_upsert_and_get_node, test_add_edge_and_query, test_noisy_or_confidence, test_negative_evidence_reduces_confidence, test_propagation_bfs, test_propagation_max_depth, test_band_classification, test_stats; fixtures: ceg