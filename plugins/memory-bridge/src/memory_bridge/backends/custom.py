"""Custom backend adapter — the extension point for your own memory store.

Two ways to plug your store in:

1. **AML HTTP**: your server already implements the AML Add/Search contract
   (see README). Then just run the bridge with:
   `--backend custom --backend-url http://localhost:8000`
   No code needed; this adapter calls your HTTP endpoints.

2. **Python protocol**: implement `MemoryBackend` (see protocol.py) and register
   it here via `register("custom")`, or set `--backend-config` to an importable
   `module:ClassName` and we'll instantiate it.

The bridge does no intelligence — your store's retrieval/extraction/rerank/
graph strategy is what gets measured.
"""

from __future__ import annotations

import json
from typing import Any

from ..protocol import Memory, register


@register("custom")
class CustomHTTPBackend:
    """Talks to any AML Add/Search-compatible HTTP service."""

    def __init__(self, *, backend_url: str, api_key: str | None = None,
                 timeout: float = 30.0, **_: Any) -> None:
        if not backend_url:
            raise ValueError(
                "custom backend requires --backend-url "
                "(an AML Add/Search-compatible HTTP endpoint)"
            )
        self._base = backend_url.rstrip("/")
        self._add_url = f"{self._base}/add"
        self._search_url = f"{self._base}/search"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    def _client(self):
        import httpx  # lazy: trivial backend users never need it
        return httpx

    def setup(self) -> None: ...
    def reset(self, user_id: str | None = None) -> None:
        try:
            self._client().post(f"{self._base}/reset",
                       json={"user_id": user_id},
                       headers=self._headers, timeout=self._timeout)
        except Exception:
            pass

    def add(self, *, request_id: str, messages: list[dict[str, Any]],
            user_id: str, session_id: str) -> dict[str, Any]:
        r = self._client().post(self._add_url, json={
            "request_id": request_id, "messages": messages,
            "user_id": user_id, "session_id": session_id,
        }, headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def search(self, *, query: str, user_id: str,
               top_k: int = 5) -> list[Memory]:
        r = self._client().post(self._search_url, json={
            "query": query, "user_id": user_id, "top_k": top_k,
        }, headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        data = r.json().get("data", [])
        out: list[Memory] = []
        for item in data:
            out.append(Memory(
                id=str(item.get("id")),
                content=item.get("content", ""),
                score=item.get("score"),
                created_at=item.get("created_at"),
                metadata={**item.get("metadata", {}), "backend": "custom"},
            ))
        return out

    def get(self, memory_id: str) -> Memory | None:
        return None

    def list_memories(self, user_id: str, limit: int = 100) -> list[Memory]:
        return []

    def update(self, memory_id: str, content: str) -> Memory | None:
        return None

    def delete(self, memory_id: str) -> bool:
        return False


def load_custom_class(spec: str) -> Any:
    """`module.path:ClassName` -> class, for users who implement the
    MemoryBackend protocol in-process rather than over HTTP."""
    module_name, _, cls_name = spec.partition(":")
    import importlib
    mod = importlib.import_module(module_name)
    return getattr(mod, cls_name)
