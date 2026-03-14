# CoDD Scan

Scan the codebase and update the Conditioned Evidence Graph (CEG).

## Usage

Run this after code changes to update the dependency graph.

## Instructions

1. Verify `codd/codd.yaml` exists in the project root.

2. Run the scanner:
```bash
python -m codd.cli scan --path "."
```

3. Report the scan results (nodes, edges, evidence counts).

4. If the user wants details, query the graph:
```bash
python -c "
from codd.graph import CEG
ceg = CEG('codd/graph.db')
print(f'Nodes: {ceg.count_nodes()}')
print(f'Edges: {ceg.count_edges()}')
stats = ceg.stats()
print(f'Evidence: {stats[\"evidence\"]}')
ceg.close()
"
```
