# CoDD Impact Analysis

Analyze change impact from git diff using the Conditioned Evidence Graph.

## Usage

Run this to see what's affected by recent code changes.

## Instructions

1. Verify `codd/codd.yaml` and `codd/graph.db` exist.

2. Run impact analysis:
```bash
python -m codd.cli impact --diff "HEAD~1" --path "."
```

3. For specific commits:
```bash
python -m codd.cli impact --diff "<commit-hash>" --path "."
```

4. To save the report:
```bash
python -m codd.cli impact --diff "HEAD~1" --output "codd/reports/impact_$(date +%Y%m%d_%H%M%S).md"
```

5. Present the results organized by band:
   - 🟢 Green: High confidence, can auto-propagate
   - 🟡 Amber: Must review (human or AI)
   - ⬜ Gray: Informational only

6. Highlight any Convention Alerts (triggered implicit rules).
