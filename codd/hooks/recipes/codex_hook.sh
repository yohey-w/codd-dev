#!/usr/bin/env bash
# Codex post-edit hook recipe for codd propagate-from.
set -euo pipefail

raw_files="${CODEX_EDITED_FILES:-}"
if [[ -z "${raw_files}" ]]; then
  exit 0
fi

raw_files="${raw_files//$'\n'/,}"
IFS=',' read -r -a candidates <<< "${raw_files}"

files=()
for candidate in "${candidates[@]}"; do
  candidate="${candidate#"${candidate%%[![:space:]]*}"}"
  candidate="${candidate%"${candidate##*[![:space:]]}"}"
  if [[ -n "${candidate}" ]]; then
    files+=("${candidate}")
  fi
done

if ((${#files[@]} == 0)); then
  exit 0
fi

cmd=(python -m codd propagate-from --source editor_hook --editor codex)
for file in "${files[@]}"; do
  cmd+=(--files "${file}")
done

exec "${cmd[@]}"
