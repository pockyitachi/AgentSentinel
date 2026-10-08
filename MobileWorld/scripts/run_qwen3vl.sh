#!/usr/bin/env bash
set -euo pipefail

# Ordinary Qwen MobileWorld launch. Start the actor endpoint before this script.
# GUI_LEDGER_MODE=inform adds a temporary execution-state view; full also adds
# non-blocking repetition nudges. The default is off; Ledger makes no model calls.

AGENT_TYPE="${AGENT_TYPE:-qwen3vl}"
MODEL_NAME="${MODEL_NAME:-Qwen3-VL-8B-Instruct}"
LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:18007/v1}"
TASKS="${TASKS:-ALL}"
MAX_ROUND="${MAX_ROUND:-50}"
ENV_COUNT="${ENV_COUNT:-1}"
AUTO_RETRY="${AUTO_RETRY:-9}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-1}"
RUN_TAG="${RUN_TAG:-qwen3vl-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
LOG_FILE_ROOT="${LOG_FILE_ROOT:-/tmp/mobileworld-traj/${RUN_TAG}}"
AUDIT_LOG_ROOT="${AUDIT_LOG_ROOT:-/tmp/mobileworld-audit/${RUN_TAG}}"

if [[ "${START_MOBILEWORLD_ENV:-1}" == "1" ]]; then
    mw env run --count "${ENV_COUNT}" --launch-interval 20
fi

mw eval \
    --agent-type "${AGENT_TYPE}" \
    --task "${TASKS}" \
    --max-round "${MAX_ROUND}" \
    --model-name "${MODEL_NAME}" \
    --llm-base-url "${LLM_BASE_URL}" \
    --api-key "${ACTOR_API_KEY:-empty}" \
    --step-wait-time "${STEP_WAIT_TIME:-3}" \
    --max-concurrency "${MAX_CONCURRENCY}" \
    --auto-retry "${AUTO_RETRY}" \
    --log-file-root "${LOG_FILE_ROOT}" \
    --audit-log-root "${AUDIT_LOG_ROOT}" \
    --gui-ledger "${GUI_LEDGER_MODE:-off}"
