# cmd_482 CoDD Skills Frontmatter Issue #25

## Summary

Fixed GitHub Issue #25 by adding YAML frontmatter to the bundled CoDD skill
documents that were missing host CLI auto-invocation metadata.

The issue described installed package paths under `codd/_skills/`. In the
source repository, the authoritative editable files are under `skills/`.
`pyproject.toml` maps `"skills"` to `"codd/_skills"` via wheel
`force-include`, and `codd/skills_cli/discovery.py` first looks for bundled
resources under `codd._skills` before falling back to the repository `skills/`
directory.

## Commit

- Fix commit: `dc634bb27f8ef36edc05a0b446110852cb74ee66`

## Files Modified

- `skills/codd-assemble/SKILL.md`
- `skills/codd-generate/SKILL.md`
- `skills/codd-impact/SKILL.md`
- `skills/codd-init/SKILL.md`
- `skills/codd-propagate/SKILL.md`
- `skills/codd-restore/SKILL.md`
- `skills/codd-scan/SKILL.md`
- `skills/codd-validate/SKILL.md`
- `reports/cmd_482_codd_skills_frontmatter_issue25.md`

`skills/codd-evolve/SKILL.md` already had valid frontmatter and was not changed.

## Validation

```text
python3 <frontmatter-parse-check>
checked=9
frontmatter PASS: 9/9, SKIP=0

git diff --check
PASS

python3 -m pytest tests/test_skills_cli.py
14 passed, SKIP=0
```

## Scope Control

- Did not change CoDD core action-outcome logic.
- Did not touch cmd_481 or osato-lms work.
- Did not include unrelated untracked files:
  - `.codd/dag.json`
  - `docs/swebench/v2.11.0_run.md`

## Residual Risks

- This fix validates source `skills/*/SKILL.md` frontmatter and the existing
  wheel include mapping. It does not build and inspect a wheel artifact.
- Descriptions were written for natural-language auto-invocation from each
  skill's existing overview and usage sections; host-specific ranking behavior
  can still vary by CLI implementation.
