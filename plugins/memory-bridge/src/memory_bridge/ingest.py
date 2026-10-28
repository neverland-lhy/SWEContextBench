"""Bulk ingest: read SWE-ContextBench Past-Experience .jsonl trajectories and
Add them to a backend.

- `--summarize llm`   : generate ~217-token LLM summaries (gpt-4o-mini) and
  store those (Free Summary pool).
- `--summarize field` : use the .jsonl's built-in short `summary` field.
- (default)           : store the full trajectory text (Free Context pool).

`user_id` is set per repo (the trajectory's instance_id repo part) so search
is isolated to the same project, matching the paper's "same-repo experience".
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from .protocol import get_backend
from .summarize import summarize_text
from .trajectory import Trajectory, iter_trajectories, trajectory_text


def _repo_of(instance_id: str | None) -> str:
    if not instance_id:
        return "unknown"
    # e.g. "django__django-12915" -> "django"
    head = instance_id.split("__", 1)[0]
    return head or "unknown"


def ingest(
    *,
    glob_pattern: str,
    backend: str,
    summarize: str | None = None,  # None | "llm" | "field"
    backend_url: str | None = None,
    store_path: str | None = None,
    limit: int | None = None,
    summary_cache_dir: str | None = None,
    llm_model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if backend == "custom" and backend_url:
        kw["backend_url"] = backend_url
    if backend == "mem0" and backend_url:
        kw["base_url"] = backend_url
    if backend == "trivial" and store_path:
        kw["store_path"] = store_path
    b = get_backend(backend, **kw)
    n = 0
    for traj in iter_trajectories(glob_pattern):
        if limit is not None and n >= limit:
            break
        user_id = _repo_of(traj.instance_id)
        rid = str(uuid.uuid4())
        if summarize == "field":
            body = traj.summary or trajectory_text(traj)[:1000]
        elif summarize == "llm":
            body = summarize_text(
                trajectory_text(traj),
                model=llm_model,
                cache_dir=summary_cache_dir,
            )
        else:
            body = trajectory_text(traj)
        b.add(
            request_id=rid,
            messages=[{"role": "assistant", "content": body}],
            user_id=user_id,
            session_id=os.path.basename(traj.path),
        )
        n += 1
        if n % 25 == 0:
            print(f"  ingested {n} trajectories")
    return {"ingested": n, "backend": backend, "summarize": summarize}
