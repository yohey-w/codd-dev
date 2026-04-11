---
codd:
  node_id: req:clustering
  type: requirement
  depends_on: []
  confidence: 1.0
  source: codd-require
---

# Clustering — Inferred Requirements

## 1. Overview

The clustering module (`codd/clustering.py`, 168 lines) provides automatic feature-cluster discovery for the `codd extract` pipeline. It groups project modules into semantically related clusters using two complementary heuristics: **call-graph connectivity** and **naming-prefix similarity**. The module is classified as infrastructure, serving the `extractor` module which invokes it during project fact extraction. The module is referenced in the codebase as requirement **R4.2** (per the docstring `R4.2 — Feature clustering for codd extract`). [observed]

## 2. Functional Requirements

### FR-1: Build Feature Clusters from Project Facts [observed]
The system SHALL accept a `ProjectFacts` object and populate its `feature_clusters` field with a list of `FeatureCluster` objects.
- **Evidence**: `build_feature_clusters(facts: ProjectFacts) -> None` at `codd/clustering.py:16`; the function mutates `facts.feature_clusters` at line 86.

### FR-2: Skip Clustering for Trivial Projects [observed]
The system SHALL return immediately without producing clusters when fewer than 2 modules exist.
- **Evidence**: Early return at `codd/clustering.py:21-22`; tested by `test_build_feature_clusters_single_module`.

### FR-3: Call-Graph-Based Clustering [observed]
The system SHALL build an undirected adjacency graph from `call_edges` on each module, resolve callee references to known module names, and group modules into connected components.
- **Evidence**: Steps 1-2 at `codd/clustering.py:25-35`; adjacency is bidirectional (line 31-32); BFS connected-component discovery at `_connected_components` (line 106-128).

### FR-4: Callee Module Resolution [observed]
The system SHALL resolve dotted callee identifiers (e.g., `auth.verify_token`) to known module names by attempting:
1. Exact match against the module name list.
2. Longest prefix match using dot-separated segments.
3. First segment match as a fallback.
4. Return `None` if no match is found.
- **Evidence**: `_resolve_callee_module` at `codd/clustering.py:89-103`; tested by `test_resolve_callee_module_exact`, `test_resolve_callee_module_dotted`, `test_resolve_callee_module_unknown`.

### FR-5: Naming-Prefix-Based Clustering [observed]
The system SHALL group modules by shared underscore/dot-delimited prefix as a secondary heuristic. Prefixes shorter than 2 characters and groups with fewer than 2 members are excluded.
- **Evidence**: `_group_by_prefix` at `codd/clustering.py:131-142`; tested by `test_group_by_prefix`.

### FR-6: Cluster Merging with Priority [observed]
The system SHALL prioritize call-graph clusters over prefix-only clusters. Modules already assigned to a call-graph cluster SHALL NOT appear in prefix-only clusters.
- **Evidence**: `seen` set at `codd/clustering.py:42`; prefix-only loop filters by `m not in seen` at line 75.

### FR-7: Confidence Scoring [observed]
The system SHALL assign a confidence score to each cluster:
- **Call-graph clusters**: `min(1.0, 0.4 + 0.1 * edge_count + 0.2 if common_prefix)` — starting at 0.4, increasing with cross-call edge count and shared naming prefix.
- **Prefix-only clusters**: fixed confidence of 0.3.
- **Evidence**: `codd/clustering.py:63` and `codd/clustering.py:81`.

### FR-8: Evidence Recording [observed]
Each cluster SHALL carry a list of human-readable evidence strings describing why modules were grouped (e.g., `"shared prefix: auth"`, `"2 cross-call edges"`).
- **Evidence**: `evidence` list construction at `codd/clustering.py:49-61`, `codd/clustering.py:82`.

### FR-9: Cluster Naming [observed]
The system SHALL infer a cluster name from the common prefix of its members, falling back to the shortest module name when no common prefix exists.
- **Evidence**: `_infer_cluster_name` at `codd/clustering.py:162-168`.

### FR-10: Output Ordering [observed]
The system SHALL sort clusters by descending confidence before assigning to `facts.feature_clusters`. Module lists within each cluster SHALL be sorted alphabetically.
- **Evidence**: `sorted(clusters, key=lambda c: -c.confidence)` at line 86; `sorted(comp)` at line 67 and `sorted(remaining)` at line 79.

## 3. Non-Functional Requirements

### NFR-1: Full Test Coverage [observed]
The module has 100% symbol coverage (1/1 public function covered). Nine test cases exercise all internal helper functions and the three main clustering scenarios (call-graph, prefix, single-module).
- **Evidence**: Coverage metric `1.0` in the extracted module doc; `tests/test_clustering.py` with 9 test functions.

### NFR-2: Low Change Risk [observed]
The module has a change-risk score of 0.23 (lowest tier), indicating low coupling and full test coverage.
- **Evidence**: Architecture overview risk table — `clustering: 0.23`.

### NFR-3: Minimal External Dependencies [observed]
The module uses only Python standard library (`collections.defaultdict`, `typing.TYPE_CHECKING`) and internal project types (`ProjectFacts`, `FeatureCluster`, `CallEdge`). No third-party packages are required.
- **Evidence**: Import statements at `codd/clustering.py:1-13`.

### NFR-4: In-Place Mutation Pattern [observed]
The module modifies the `ProjectFacts` object in place rather than returning a new value, following the same pattern as other extractor sub-modules (contracts, env_refs, risk, etc.).
- **Evidence**: Return type `None`; `facts.feature_clusters = ...` at line 86.

## 4. Constraints

### C-1: Dependency on Extractor Types [observed]
The module imports `ProjectFacts` and `FeatureCluster` from `codd.extractor`. This creates a bidirectional dependency (`extractor -> clustering` and `clustering -> extractor`), mitigated by `TYPE_CHECKING` guard for the forward reference.
- **Evidence**: `codd/clustering.py:10-13` (TYPE_CHECKING import), `codd/clustering.py:18` (runtime import inside function).

### C-2: Python Language Constraint [observed]
The module is pure Python with no platform-specific dependencies.
- **Evidence**: Module source uses only standard library constructs.

### C-3: Undirected Graph Model [observed]
Call relationships are treated as undirected edges for clustering purposes — if module A calls module B, both are considered related bidirectionally. This is an explicit design choice that maximizes cluster cohesion.
- **Evidence**: Both `adj[mod.name].add(target_mod)` and `adj[target_mod].add(mod.name)` at lines 31-32.

### C-4: Singleton Exclusion [observed]
Connected components and prefix groups containing only a single module are silently discarded; they do not produce clusters.
- **Evidence**: `if len(comp) < 2: continue` at line 46; `if len(remaining) < 2: continue` at line 76; prefix group filter at line 142.

## 5. Open Questions

1. **[speculative] Cross-reference density**: The module docstring mentions "cross-reference density" as a clustering signal, but the implementation only uses call edges and naming prefixes. Was a density-based heuristic planned but not implemented, or does "cross-reference density" refer to the edge-count component of the confidence formula?

2. **[speculative] Confidence thresholds**: The confidence values (0.3, 0.4 base, 0.1 per edge, 0.2 for prefix) appear to be manually tuned constants. Were these derived from empirical testing on real projects, or are they initial guesses awaiting calibration?

3. **[inferred] Cluster consumption**: The architecture overview renders clusters in the "Feature Clusters" section, but it is unclear whether any downstream module (planner, generator, etc.) uses cluster data for decision-making beyond documentation. Needs human confirmation of whether clusters influence code generation or planning.

4. **[speculative] Dot-separated module names**: The prefix logic handles both underscore and dot separators (`name.replace(".", "_").split("_")`). Is this intended to support namespaced/nested module names (e.g., `auth.service`), or is it a defensive measure?
