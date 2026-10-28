#!/usr/bin/env bash
# PostToolUse hook (mem0-style live capture): after Read/Write/Edit/Bash,
# record the tool event into memory so backends with a "build-as-you-go"
# strategy can use the session activity.
#
# Hook receives JSON on stdin: { tool_name, tool_input, tool_response }
# We forward a compact {role, content} to memory-bridge add.

set -eu
BACKEND="${MB_BACKEND:-trivial}"
STORE="${MB_STORE_PATH:-memory_store.jsonl}"
URL="${MB_BACKEND_URL:-}"
USER_ID="${MB_USER_ID:-default}"

INPUT="$(cat)"
PAYLOAD="$(python3 - "$INPUT" <<'PY'
import json, sys
try: o=json.load(sys.stdin)
except Exception: sys.exit(0)
name=o.get("tool_name","")
if name not in ("Read","Write","Edit","MultiEdit","Bash"): sys.exit(0)
ti=o.get("tool_input",{}) or {}
resp=o.get("tool_response","")
if isinstance(resp,dict): resp=json.dumps(resp)[:1500]
else: resp=str(resp)[:1500]
content=f"[{name}] input={json.dumps(ti)[:800]} output={resp[:1200]}"
print(json.dumps({"role":"assistant","content":content}))
PY
)"
[ -z "$PAYLOAD" ] && exit 0

TMP="$(mktemp)"
printf '[%s]' "$PAYLOAD" > "$TMP"
ARGS=(memory-bridge add --messages "$TMP" --user-id "$USER_ID" --session-id posttool --backend "$BACKEND" --store-path "$STORE")
[ -n "$URL" ] && ARGS+=(--backend-url "$URL")
"${ARGS[@]}" >/dev/null 2>&1 || true
rm -f "$TMP"
exit 0
