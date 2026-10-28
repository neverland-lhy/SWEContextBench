#!/usr/bin/env bash
# PreToolUse hook: block Claude from writing its OWN native memory files.
# Forces all memory through the memory-bridge `add_memory` tool instead.
# (Mirrors mem0-plugin's approach of routing memory away from native storage.)
#
# Claude Code calls this with JSON on stdin:
#   { "tool_name": "Write"|"Edit", "tool_input": { "file_path": "..." } }
# We emit a JSON "deny" decision when the target is a native-memory path.

set -eu
INPUT="$(cat)"

# extract tool_name + file_path via python (hook scripts must be robust)
DECISION=$(python3 - "$INPUT" <<'PY'
import json, sys, re
try:
    o = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
name = o.get("tool_name", "")
path = (o.get("tool_input") or {}).get("file_path", "")
if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
    sys.exit(0)

def is_native_memory(p):
    if p is None:
        return False
    p = str(p)
    base = p.rsplit("/", 1)[-1]
    if base in ("MEMORY.md", "CLAUDE.md", "summary.md"):
        return True
    if re.search(r"/\.claude/projects/[^/]+/memory/", p):
        return True
    if "/.claude/projects/" in p and "/memory/" in p:
        return True
    return False

if is_native_memory(path):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Native memory writes are disabled for this benchmark. Use the `add_memory` MCP tool instead."
        }
    }))
PY
)
if [ -n "$DECISION" ]; then
    echo "$DECISION"
fi
exit 0
