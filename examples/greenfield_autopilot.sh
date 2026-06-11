#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# greenfield_autopilot.sh — CoDD greenfield autopilot, composed from the
# stage-level CLI commands.
#
# EQUIVALENT ONE-COMMAND FORM (preferred when you just want the result):
#
#     codd greenfield --requirements docs/requirements/requirements.md \
#         [--project-name NAME --language LANG --ntfy-topic TOPIC]
#
# `codd greenfield` runs the SAME stage sequence in-process, with
# checkpoint/resume (.codd/greenfield_session.yaml), `--dry-run` previews and
# optional ntfy progress notifications. This script exists for users who want
# the pipeline as transparent shell composition — to read it, audit it, splice
# stages into their own CI, or drive CoDD from an AI CLI we do not ship a
# skill for. Stage boundaries here are 1:1 with the autopilot's stages, so
# anything this script does, `codd greenfield` does too (and vice versa).
#
# Stage sequence (identical to `codd greenfield`):
#
#   1. init       codd init NAME --language LANG [--requirements FILE] --auto-approve
#                 (skipped when codd/codd.yaml or .codd/codd.yaml already exists)
#   2. elicit     codd elicit  +  codd elicit apply findings.md
#                 (ADVISORY — finds requirement gaps; failure never blocks)
#   3. plan       codd plan --init --force
#   4. generate   codd generate --all-waves --force
#   5. implement  codd implement list-tasks --format json, then per task:
#                   codd implement plan  --task T          (advisory)
#                   codd implement steps --task T --approve --all   (advisory)
#                   codd implement run   --task T          (blocking)
#                 (when no tasks exist: codd plan derive + codd plan approve)
#   6. verify     codd verify --auto-repair --max-attempts N --repair-mode automatic
#   7. propagate  codd propagate --verify && codd propagate --commit  (advisory)
#   8. check      codd check   — the final health gate; its exit code is ours
#
# AI-CLI agnostic: pass any text-in/text-out CLI via --ai-cmd
# (e.g. --ai-cmd 'claude --print', --ai-cmd 'codex exec', or your own wrapper).
# Nothing here assumes which AI CLI sits underneath.
#
# Usage:
#   greenfield_autopilot.sh PROJECT_NAME [options]
#
#   PROJECT_NAME                project name for codd init (required unless
#                               the current directory is already initialized)
#   --language LANG             primary language for codd init (default: python)
#   --requirements FILE         requirements document to import (any format)
#   --ai-cmd 'CMD'              override the AI CLI command for every stage
#   --max-repair N              max automatic repair attempts (default: 10)
#   -h | --help                 show this help
#
# Run it from the directory that should become (or already is) the project
# root. Requires: codd on PATH, python3 or jq for JSON parsing.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

usage() {
    sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'
}

# ── argument parsing ────────────────────────────────────────────────────────
PROJECT_NAME=""
LANGUAGE="python"
REQUIREMENTS=""
AI_CMD=""
MAX_REPAIR=10

while [[ $# -gt 0 ]]; do
    case "$1" in
        --language)
            LANGUAGE="${2:?--language needs a value}"; shift 2 ;;
        --requirements)
            REQUIREMENTS="${2:?--requirements needs a value}"; shift 2 ;;
        --ai-cmd)
            AI_CMD="${2:?--ai-cmd needs a value}"; shift 2 ;;
        --max-repair)
            MAX_REPAIR="${2:?--max-repair needs a value}"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        -*)
            echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)
            if [[ -z "${PROJECT_NAME}" ]]; then
                PROJECT_NAME="$1"; shift
            else
                echo "unexpected argument: $1" >&2; exit 2
            fi ;;
    esac
done

if [[ -n "${REQUIREMENTS}" && ! -f "${REQUIREMENTS}" ]]; then
    echo "requirements file not found: ${REQUIREMENTS}" >&2
    exit 2
fi

# --ai-cmd plumbing: passed explicitly to every stage command that accepts it,
# and exported as CODD_AI_COMMAND for the components that read the environment
# (e.g. the verify repair loop). codd.yaml ai_command remains the default.
AI_ARGS=()
if [[ -n "${AI_CMD}" ]]; then
    AI_ARGS=(--ai-cmd "${AI_CMD}")
    export CODD_AI_COMMAND="${AI_CMD}"
fi

# On any failure, point at the checkpoint/resume story of the one-command form.
trap 'status=$?; if [[ ${status} -ne 0 ]]; then
    echo "[autopilot] FAILED (exit ${status})." >&2
    echo "[autopilot] tip: the equivalent one-command form checkpoints every unit" >&2
    echo "[autopilot]      and can resume:  codd greenfield --resume" >&2
fi' EXIT

step() { echo; echo "═══ [autopilot] $* ═══"; }

# Parse task ids out of `codd implement list-tasks --format json`.
# Prefers python3 (ships with CoDD installs), falls back to jq.
parse_task_ids() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json, sys
for task in json.load(sys.stdin):
    print(task["task_id"])'
    elif command -v jq >/dev/null 2>&1; then
        jq -r '.[].task_id'
    else
        echo "[autopilot] need python3 or jq to parse the task list" >&2
        return 1
    fi
}

# ── stage 1: init (idempotent — skip when already initialized) ──────────────
step "stage 1/8: init"
if [[ -e codd/codd.yaml || -e .codd/codd.yaml ]]; then
    echo "[autopilot] CoDD config dir already exists — init skipped"
else
    if [[ -z "${PROJECT_NAME}" ]]; then
        echo "PROJECT_NAME is required when the directory is not yet initialized" >&2
        exit 2
    fi
    INIT_ARGS=(init "${PROJECT_NAME}" --language "${LANGUAGE}" --auto-approve)
    if [[ -n "${REQUIREMENTS}" ]]; then
        INIT_ARGS+=(--requirements "${REQUIREMENTS}")
    fi
    codd "${INIT_ARGS[@]}"
fi

# ── stage 2: elicit (ADVISORY — never blocks the autopilot) ─────────────────
# `codd elicit` writes findings.md at the project root; `elicit apply` folds
# the findings back into requirements/lexicon. Mirrors the autopilot, where
# any elicit failure degrades to a warning.
step "stage 2/8: elicit (advisory)"
if codd elicit "${AI_ARGS[@]}"; then
    if [[ -f findings.md ]]; then
        codd elicit apply findings.md || echo "[autopilot] WARNING: elicit apply failed (non-blocking)"
    fi
else
    echo "[autopilot] WARNING: elicit failed (non-blocking) — continuing"
fi

# ── stage 3: plan — derive wave_config from the requirement docs ────────────
step "stage 3/8: plan --init"
codd plan --init --force "${AI_ARGS[@]}"

# ── stage 4: generate — every wave, in order, stop at first failure ─────────
step "stage 4/8: generate --all-waves"
codd generate --all-waves --force "${AI_ARGS[@]}"

# ── stage 5: implement — deterministic task loop ────────────────────────────
step "stage 5/8: implement"
TASKS_JSON="$(codd implement list-tasks --format json)"
TASK_IDS=()
while IFS= read -r task_id; do
    [[ -n "${task_id}" ]] && TASK_IDS+=("${task_id}")
done < <(printf '%s' "${TASKS_JSON}" | parse_task_ids)

if [[ ${#TASK_IDS[@]} -eq 0 ]]; then
    # No configured targets and no approved derived tasks yet: derive tasks
    # from the design docs and auto-approve them (the autopilot equivalent of
    # the HITL task-approval gate), then re-list.
    echo "[autopilot] no implement tasks found — deriving from design docs"
    codd plan derive "${AI_ARGS[@]}"
    while IFS= read -r design_doc; do
        [[ -n "${design_doc}" ]] && codd plan approve "${design_doc}" --all
    done < <(find docs/design -name '*.md' 2>/dev/null || true)
    TASKS_JSON="$(codd implement list-tasks --format json)"
    while IFS= read -r task_id; do
        [[ -n "${task_id}" ]] && TASK_IDS+=("${task_id}")
    done < <(printf '%s' "${TASKS_JSON}" | parse_task_ids)
fi

if [[ ${#TASK_IDS[@]} -eq 0 ]]; then
    echo "[autopilot] no implement tasks found: declare implement.default_output_paths" >&2
    echo "[autopilot] in codd.yaml or check that design docs support task derivation" >&2
    exit 1
fi

echo "[autopilot] ${#TASK_IDS[@]} task(s) to implement"
for task_id in "${TASK_IDS[@]}"; do
    echo "[autopilot] implement task: ${task_id}"
    # Step derivation + approval are advisory (implementation works without
    # derived steps) — mirror the autopilot and never block on them.
    codd implement plan --task "${task_id}" "${AI_ARGS[@]}" \
        || echo "[autopilot] WARNING: implement plan failed for ${task_id} (non-blocking)"
    codd implement steps --task "${task_id}" --approve --all \
        || echo "[autopilot] WARNING: step approval failed for ${task_id} (non-blocking)"
    # The run itself is blocking: a failed task fails the autopilot.
    codd implement run --task "${task_id}" "${AI_ARGS[@]}"
done

# ── stage 6: verify — automatic repair loop, no HITL ────────────────────────
# --repair-mode automatic is the explicit opt-in for unattended repair
# approval; oversized proposals still escalate (and fail) as a safety valve.
step "stage 6/8: verify --auto-repair"
codd verify --auto-repair --max-attempts "${MAX_REPAIR}" --repair-mode automatic

# ── stage 7: propagate (ADVISORY on a fresh build) ──────────────────────────
# "Nothing to propagate" (no git repo / no changed files) is normal on a
# fresh build, so failures degrade to warnings — same as the autopilot.
step "stage 7/8: propagate (advisory)"
codd propagate --verify || echo "[autopilot] WARNING: propagate --verify skipped (non-blocking)"
codd propagate --commit || echo "[autopilot] WARNING: propagate --commit skipped (non-blocking)"

# ── stage 8: check — the final health gate ──────────────────────────────────
# This is the last command on purpose: its exit code is the script's exit
# code (set -e propagates any earlier blocking failure the same way).
step "stage 8/8: check"
codd check

echo
echo "[autopilot] SUCCESS — requirements in, system out."
