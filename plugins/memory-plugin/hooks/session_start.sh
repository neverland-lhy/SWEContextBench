#!/usr/bin/env bash
# SessionStart hook (mem0-style auto-inject): retrieve relevant past experience
# for the current task and surface it as context before the agent acts.
#
# Env (set by Harbor / orchestrator):
#   MB_BACKEND      - backend name (trivial|mem0|custom)
#   MB_STORE_PATH   - trivial store path (if trivial)
#   MB_BACKEND_URL  - custom/mem0 endpoint (if any)
#   MB_USER_ID      - repo name (retrieval scope)
#   MB_QUERY        - the task problem statement (or read from /task/problem.txt)

set -eu
BACKEND="${MB_BACKEND:-trivial}"
STORE="${MB_STORE_PATH:-memory_store.jsonl}"
URL="${MB_BACKEND_URL:-}"
USER_ID="${MB_USER_ID:-default}"
QUERY="${MB_QUERY:-}"
[ -z "$QUERY" ] && [ -f /task/problem.txt ] && QUERY="$(cat /task/problem.txt)"
[ -z "$QUERY" ] && exit 0

ARGS=(memory-bridge search "$QUERY" --user-id "$USER_ID" --top-k 5 --backend "$BACKEND" --store-path "$STORE" --raw)
[ -n "$URL" ] && ARGS+=(--backend-url "$URL")

MEMS="$( "${ARGS[@]}" 2>/dev/null || true )"
if [ -n "$MEMS" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Relevant past experience from memory:\\n\\n%s\\n\\nUse this to inform your fix."}}\n' "$MEMS"
fi
exit 0
