"""Trivial file-backed backend: BM25 over ingested messages.

No external deps. A lower-bound baseline. Persists to a JSONL file so that
`ingest` (one process) and `serve-mcp` / `search` (another process) share
state. Implements the full :class:`~memory_bridge.protocol.MemoryBackend`
contract.

No LLM, no rerank, no extraction — pure keyword retrieval. This is the
"no intelligence" reference point: if your backend can't beat trivial on
SWE-ContextBench, its strategy adds nothing.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from typing import Any

from ..protocol import Memory, register

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@register("trivial")
class TrivialBackend:
    def __init__(self, *, store_path: str = "memory_store.jsonl",
                 **_: Any) -> None:
        self._path = store_path
        os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
        self._docs = self._load()

    # ---- persistence ----
    def _load(self) -> list[Memory]:
        docs: list[Memory] = []
        if not os.path.exists(self._path):
            return docs
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                docs.append(Memory(
                    id=o["id"], content=o["content"],
                    created_at=o.get("created_at"),
                    metadata=o.get("metadata", {}),
                ))
        return docs

    def _append(self, mem: Memory) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": mem.id, "content": mem.content,
                "created_at": mem.created_at, "metadata": mem.metadata,
            }, ensure_ascii=False) + "\n")

    def _save_all(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for m in self._docs:
                if m.id == "__deleted__":
                    continue
                f.write(json.dumps({
                    "id": m.id, "content": m.content,
                    "created_at": m.created_at, "metadata": m.metadata,
                }, ensure_ascii=False) + "\n")
        os.replace(tmp, self._path)

    # ---- BM25 over current docs, scoped to user_id ----
    def _search(self, query: str, user_id: str, top_k: int) -> list[Memory]:
        user_docs = [m for m in self._docs
                     if m.metadata.get("user_id") == user_id]
        if not user_docs:
            return []
        q_toks = _tokenize(query)
        if not q_toks:
            return []
        N = len(user_docs)
        avgdl = (sum(len(_tokenize(m.content)) for m in user_docs) / N) or 1.0
        k1, b = 1.5, 0.75
        # df per token
        df: dict[str, int] = defaultdict(int)
        tokenized: list[list[str]] = []
        for m in user_docs:
            toks = _tokenize(m.content)
            tokenized.append(toks)
            for tok in set(toks):
                df[tok] += 1
        scores: list[tuple[int, float]] = []
        for i, toks in enumerate(tokenized):
            tf = Counter(toks)
            dl = len(toks) or 1
            s = 0.0
            for tok in q_toks:
                if tok not in tf:
                    continue
                idf = math.log((N - df[tok] + 0.5) / (df[tok] + 0.5) + 1.0)
                s += idf * (tf[tok] * (k1 + 1)) / (tf[tok] + k1 * (1 - b + b * dl / avgdl))
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda kv: -kv[1])
        out: list[Memory] = []
        for i, s in scores[:top_k]:
            m = user_docs[i]
            out.append(Memory(
                id=m.id, content=m.content, score=float(s),
                created_at=m.created_at,
                metadata={**m.metadata, "backend": "trivial"},
            ))
        return out

    # ---- lifecycle ----
    def setup(self) -> None: ...
    def reset(self, user_id: str | None = None) -> None:
        if user_id is None:
            self._docs = []
            self._save_all()
        else:
            self._docs = [m for m in self._docs
                          if m.metadata.get("user_id") != user_id]
            self._save_all()

    # ---- AML Add ----
    def add(self, *, request_id: str, messages: list[dict[str, Any]],
            user_id: str, session_id: str) -> dict[str, Any]:
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            parts.append(f"{role}: {content}")
        body = "\n".join(parts)
        mid = f"trivial-{user_id}-{len(self._docs)}-{int(time.time())}"
        mem = Memory(
            id=mid, content=body,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            metadata={
                "user_id": user_id, "session_id": session_id,
                "request_id": request_id, "backend": "trivial",
            },
        )
        self._docs.append(mem)
        self._append(mem)
        return {
            "success": True, "request_id": request_id,
            "user_id": user_id, "session_id": session_id,
            "memory_id": mid,
        }

    # ---- AML Search ----
    def search(self, *, query: str, user_id: str,
               top_k: int = 5) -> list[Memory]:
        # reload to pick up writes from other processes
        self._docs = self._load()
        return self._search(query, user_id, top_k)

    # ---- management ----
    def get(self, memory_id: str) -> Memory | None:
        self._docs = self._load()
        for m in self._docs:
            if m.id == memory_id:
                return m
        return None

    def list_memories(self, user_id: str, limit: int = 100) -> list[Memory]:
        self._docs = self._load()
        return [m for m in self._docs
                if m.metadata.get("user_id") == user_id][:limit]

    def update(self, memory_id: str, content: str) -> Memory | None:
        self._docs = self._load()
        for m in self._docs:
            if m.id == memory_id:
                m.content = content
                self._save_all()
                return m
        return None

    def delete(self, memory_id: str) -> bool:
        self._docs = self._load()
        for m in self._docs:
            if m.id == memory_id:
                self._docs.remove(m)
                self._save_all()
                return True
        return False
