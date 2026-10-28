"""Backend protocol + registry.

A backend is the ONLY thing that varies across memory strategies. The bridge,
MCP tools, skill and hooks stay fixed so score differences reflect strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Memory:
    """A single retrievable memory item (AML Search response element)."""

    id: str
    content: str
    score: float | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MemoryBackend(Protocol):
    """Backend contract. Mirrors the AML Add/Search HTTP schema.

    `user_id` is the sole retrieval-isolation field: a search only ever sees
    memories added under the same user_id. Map user_id to repo / project.
    """

    # --- lifecycle (optional) ---
    def setup(self) -> None: ...
    def reset(self, user_id: str | None = None) -> None: ...

    # --- AML Add ---
    def add(
        self,
        *,
        request_id: str,
        messages: list[dict[str, Any]],
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist messages; must be immediately searchable before returning.

        Returns AML Add response: {success, request_id, user_id, session_id}.
        """
        ...

    # --- AML Search ---
    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> list[Memory]:
        """Return ranked memories for the query, scoped to user_id."""
        ...

    # --- management (optional; default to no-op / NotImplemented) ---
    def get(self, memory_id: str) -> Memory | None: ...
    def list_memories(self, user_id: str, limit: int = 100) -> list[Memory]: ...
    def update(self, memory_id: str, content: str) -> Memory | None: ...
    def delete(self, memory_id: str) -> bool: ...


_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Decorator to register a backend class under `name`."""

    def deco(cls: type) -> type:
        _REGISTRY[name] = cls
        return cls

    return deco


def get_backend(name: str, **kwargs: Any) -> MemoryBackend:
    """Instantiate a registered backend by name."""
    if name not in _REGISTRY:
        # make sure submodules are imported so decorators run
        from . import backends as _b  # noqa: F401
        _b.load()  # type: ignore[attr-defined]
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown backend {name!r}. Registered: {sorted(_REGISTRY)}"
        )
    backend = _REGISTRY[name](**kwargs)
    backend.setup()
    return backend
