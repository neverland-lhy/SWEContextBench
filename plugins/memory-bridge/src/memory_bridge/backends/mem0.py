"""Mem0 backend adapter.

Wraps the Mem0 SDK so Mem0's own LLM extraction + rerank count as its strategy.
The bridge still does no intelligence; Mem0's strategy lives inside `mem0.add()`
and `mem0.search()`.

Requires: `pip install mem0ai` and Mem0 env (MEM0_API_KEY or local config).
At runtime pass `--backend mem0 [--backend-url URL] [--backend-config yaml]`.
"""

from __future__ import annotations

import json
from typing import Any

from ..protocol import Memory, register


@register("mem0")
class Mem0Backend:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 config_path: str | None = None, default_user_id: str = "bench",
                 **_: Any) -> None:
        try:
            from mem0 import Memory as Mem0Memory  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "mem0 backend requires `pip install mem0ai`"
            ) from e
        cfg: dict[str, Any] = {}
        if api_key:
            cfg["api_key"] = api_key
        if base_url:
            cfg["host"] = base_url
        # Mem0 Memory.from_config or Memory(config=...) depending on version.
        if config_path:
            with open(config_path) as f:
                cfg = json.load(f) if config_path.endswith(".json") else {
                    **cfg, **_yaml_load(f)}
        self._m = Mem0Memory.from_config(cfg) if cfg else Mem0Memory()
        self._default_user = default_user_id

    def setup(self) -> None: ...
    def reset(self, user_id: str | None = None) -> None:
        if user_id is None:
            return
        try:
            self._m.delete_all(user_id=user_id)
        except Exception:
            pass

    def add(self, *, request_id: str, messages: list[dict[str, Any]],
            user_id: str, session_id: str) -> dict[str, Any]:
        res = self._m.add(messages, user_id=user_id, metadata={
            "session_id": session_id, "request_id": request_id,
        })
        return {
            "success": True, "request_id": request_id,
            "user_id": user_id, "session_id": session_id,
            "mem0_result": res,
        }

    def search(self, *, query: str, user_id: str,
               top_k: int = 5) -> list[Memory]:
        res = self._m.search(query, user_id=user_id, limit=top_k)
        out: list[Memory] = []
        for item in res.get("results", res) if isinstance(res, dict) else res:
            out.append(Memory(
                id=str(item.get("id")),
                content=item.get("memory", item.get("content", "")),
                score=item.get("score"),
                created_at=item.get("created_at"),
                metadata={**item.get("metadata", {}), "backend": "mem0"},
            ))
        return out

    def get(self, memory_id: str) -> Memory | None:
        return None

    def list_memories(self, user_id: str, limit: int = 100) -> list[Memory]:
        res = self._m.get_all(user_id=user_id, limit=limit)
        out = []
        for item in (res.get("results", res) if isinstance(res, dict) else res):
            out.append(Memory(
                id=str(item.get("id")),
                content=item.get("memory", ""),
                metadata=item.get("metadata", {}),
            ))
        return out[:limit]

    def update(self, memory_id: str, content: str) -> Memory | None:
        self._m.update(memory_id, content)
        return None

    def delete(self, memory_id: str) -> bool:
        self._m.delete(memory_id)
        return True


def _yaml_load(f):  # pragma: no cover
    import yaml
    return yaml.safe_load(f)
