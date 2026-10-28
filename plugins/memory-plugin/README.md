# memory-plugin

The fixed ① + ② layer of the three-layer design: a Claude Code / Harbor plugin
bundle that exposes the memory-bridge MCP tools and the mem0-style lifecycle
hooks. **Mirror of mem0's plugin design** — copy `mem0ai/mem0/.claude-plugin`
verbatim to cross-check tool names / hook events before final tuning.

## Layout
```
skills/memory/SKILL.md       behavior: search_memories before fixing; do NOT add
hooks/
  pretool_block_native_memory.sh  PreToolUse: deny native MEMORY.md writes
  session_start.sh                SessionStart: auto-inject recalled memory (read-only)
  posttool_capture.sh             (unused) was PostToolUse live-capture — removed
  stop_save.sh                    (unused) was Stop save-session — removed
settings.snippet.json          MCP + hooks declaration (Free/Auto-inject only)
baseline_hygiene.json          permissions + AskUserQuestion deny (ALL settings)
```

## Mounting (per setting)
- **No-Context**: `baseline_hygiene.json` only. No MCP, no skill, no hooks.
- **Free Context / Free Summary**: baseline + `settings.snippet.json` +
  skills_dir → this dir. Backend set via the MCP `--backend` arg + hook env
  (`MB_BACKEND`, `MB_STORE_PATH`, `MB_BACKEND_URL`, `MB_USER_ID`).

## Swapping the backend
Change only `--backend` in `settings.snippet.json` mcpServers.args AND the
`MB_BACKEND`/`MB_BACKEND_URL` hook env (the orchestrator sets both from one
place). The skill and tool surface stay identical → fair strategy comparison.
