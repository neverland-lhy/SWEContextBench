# memory-bridge

Universal memory bridge for SWE-ContextBench. Exposes a **mem0-style MCP tool
surface** to the agent (`add_memory`, `search_memories`, ...) and routes every
call to a **swappable backend** behind the AML Add/Search contract.

Design rules (fairness):
- The bridge, the MCP tools, the skill and the hooks are **fixed** across
  backends. Only the backend varies — so score differences reflect memory
  *strategy*, not agent behaviour.
- The bridge does **zero** LLM extraction / reranking. All intelligence lives in
  the backend (that is what we are measuring).

## Backends
- `trivial` — in-process TF-IDF baseline, no external deps. Lower bound.
- `mem0`   — wraps the Mem0 SDK / API (Mem0's extraction + rerank count as
  its strategy).
- `custom` — your own store. Either expose AML Add/Search HTTP and pass
  `--backend-url`, or implement `MemoryBackend` and register it.

## Backend contract (AML Add/Search)
```
add(request_id, messages: [{role, content, timestamp?}], user_id, session_id)
    -> {"success": True, "request_id": ..., "user_id": ..., "session_id": ...}
search(query, user_id, top_k=100)
    -> [{"id", "content", "score?", "created_at?"}, ...]
```
`user_id` is the sole retrieval-isolation field (map it to repo / project).

## CLI
```
memory-bridge serve-mcp --backend trivial
memory-bridge add   --backend trivial --user-id django --messages '...'
memory-bridge search--backend trivial --user-id django --query "..." --top-k 5
memory-bridge ingest  --backend trivial --from "cases/...Past Experience/*.jsonl"
memory-bridge summarize --from "..." --out cache/summaries
```
