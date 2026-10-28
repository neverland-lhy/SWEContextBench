#!/usr/bin/env bash
# Stop hook (mem0-style save-state): when the agent stops, record a compact
# note of what it did, so future related tasks can reuse it.
set -eu
BACKEND="${MB_BACKEND:-trivial}"
STORE="${MB_STORE_PATH:-memory_store.jsonl}"
URL="${MB_BACKEND_URL:-}"
USER_ID="${MB_USER_ID:-default}"

# Stop hook payload includes the transcript path; mine a 1-line summary
INPUT="$(cat)"
NOTE="$(python3 - "$INPUT" <<'PY'
import json, sys, os
try: o=json.load(sys.stdin)
except Exception: sys.exit(0)
tp=o.get("transcript_path","")
# best-effort: pull the last assistant text from the JSONL transcript
note="agent completed a task session"
if tp and os.path.exists(tp):
    last=""
    for line in open(tp, errors="ignore"):
        try:
            j=json.loads(line)
        except Exception: continue
        if j.get("type")=="assistant":
            c=j.get("message",{}).get("content",[])
            if isinstance(c,list):
                for b in c:
                    if isinstance(b,dict) and b.get("type")=="text":
                        last=b.get("text","")
    if last: note=last[:500]
print(json.dumps({"role":"assistant","content":"[session summary] "+note}))
PY
)"
TMP="$(mktemp)"; printf '[%s]' "$NOTE" > "$TMP"
ARGS=(memory-bridge add --messages "$TMP" --user-id "$USER_ID" --session-id stop --backend "$BACKEND" --store-path "$STORE")
[ -n "$URL" ] && ARGS+=(--backend-url "$URL")
"${ARGS[@]}" >/dev/null 2>&1 || true
rm -f "$TMP"
exit 0
