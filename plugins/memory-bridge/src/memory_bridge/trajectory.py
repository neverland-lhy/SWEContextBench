"""Load SWE-ContextBench Past-Experience JSONL trajectories.

Each file is one Claude Code session. We extract:
- instance_id (from the first user message's prompt text)
- the full conversation as AML messages [{role, content, timestamp}]
- the short `summary` field if present (a fallback summary source)

Used by both `ingest` (to Add to a backend) and `summarize` (to build the
summary pool).
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass

_INSTANCE_RE = re.compile(r"instance_id:\s*([A-Za-z0-9_.\-/]+)")


@dataclass
class Trajectory:
    path: str
    instance_id: str | None
    summary: str | None
    messages: list[dict]


def _content_to_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "thinking":
                    parts.append(f"[thinking] {b.get('thinking', '')}")
                elif b.get("type") == "tool_use":
                    parts.append(
                        f"[tool_use {b.get('name')}] "
                        f"{json.dumps(b.get('input', {}))[:2000]}"
                    )
                elif b.get("type") == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(
                            x.get("text", "") if isinstance(x, dict) else str(x)
                            for x in c
                        )
                    parts.append(f"[tool_result] {str(c)[:2000]}")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content)


def parse_file(path: str) -> Trajectory:
    instance_id: str | None = None
    summary: str | None = None
    messages: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "summary":
                summary = obj.get("summary")
                continue
            if t in ("user", "assistant"):
                msg = obj.get("message", {})
                role = msg.get("role", t)
                content = _content_to_str(msg.get("content", ""))
                if not content.strip():
                    continue
                if instance_id is None and t == "user":
                    m = _INSTANCE_RE.search(content)
                    if m:
                        instance_id = m.group(1)
                messages.append(
                    {
                        "role": role,
                        "content": content,
                        "timestamp": _to_ms(obj.get("timestamp")),
                    }
                )
    return Trajectory(
        path=path,
        instance_id=instance_id,
        summary=summary,
        messages=messages,
    )


def _to_ms(ts) -> int | None:
    if ts is None:
        return None
    s = str(ts)
    # ISO 8601 -> ms
    try:
        from datetime import datetime
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


def iter_trajectories(glob_pattern: str):
    paths = sorted(glob.glob(glob_pattern, recursive=True))
    for p in paths:
        if not os.path.isfile(p):
            continue
        yield parse_file(p)


def trajectory_text(traj: Trajectory) -> str:
    """Flatten a trajectory into one text blob for summarization / storage."""
    return "\n\n".join(f"{m['role']}: {m['content']}" for m in traj.messages)
