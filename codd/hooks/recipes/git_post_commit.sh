#!/usr/bin/env bash
# Git post-commit hook recipe: run CDAP propagation from committed files.
set -euo pipefail

mapfile -t committed_files < <(git -c core.quotePath=false diff-tree --no-commit-id -r --name-only HEAD | head -20)
if ((${#committed_files[@]} == 0)); then
  exit 0
fi

cmd=(python -m codd propagate-from --source git_hook)
for file in "${committed_files[@]}"; do
  cmd+=(--files "${file}")
done

exec "${cmd[@]}"
