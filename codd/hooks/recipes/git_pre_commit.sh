#!/usr/bin/env bash
# Git pre-commit hook recipe: verify CDAP propagation from staged files.
set -euo pipefail

mapfile -t staged_files < <(git -c core.quotePath=false diff --cached --name-only --diff-filter=ACMR | head -20)
if ((${#staged_files[@]} == 0)); then
  exit 0
fi

cmd=(python -m codd propagate-from --source git_hook --dry-run)
for file in "${staged_files[@]}"; do
  cmd+=(--files "${file}")
done

exec "${cmd[@]}"
