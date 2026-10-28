"""Generate ~217-token summaries of trajectories using gpt-4o-mini.

Caches by content hash so re-runs are free. Used for the Free Summary pool
(matches the paper's "extracting the final summary section" ~217 token average,
and respects the leaderboard's gpt-4o-mini fairness constraint).
"""

from __future__ import annotations

import hashlib
import json
import os

_TARGET_TOKENS = 217  # paper average summary length

_SUMMARY_PROMPT = (
    "You are summarizing a coding agent's past session that solved a GitHub "
    "issue, to be stored as reusable experience for future similar tasks. "
    "Summarize in ~200 tokens: the bug/feature, the root cause, the fix "
    "strategy, and any reusable patterns or gotchas. Be concrete and "
    "self-contained — no filler. Output ONLY the summary."
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def summarize_text(
    text: str,
    *,
    model: str = "gpt-4o-mini",
    cache_dir: str | None = None,
    max_input_chars: int = 16000,
) -> str:
    if not text.strip():
        return ""
    # cap input
    snippet = text[:max_input_chars]
    if len(text) > max_input_chars:
        snippet += "\n...[truncated]..."

    key = _hash(snippet)
    if cache_dir:
        cache_path = os.path.join(cache_dir, f"{key}.json")
        if os.path.exists(cache_path):
            try:
                return json.load(open(cache_path))["summary"]
            except Exception:
                pass

    summary = _llm_summarize(snippet, model)

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{key}.json")
        json.dump({"summary": summary, "hash": key,
                   "model": model}, open(cache_path, "w"))
    return summary


def _llm_summarize(snippet: str, model: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover
        raise ImportError("summarize requires `pip install openai`") from e
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": snippet},
        ],
        max_tokens=_TARGET_TOKENS + 40,
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def summarize_glob(glob_pattern: str, *, out_dir: str,
                   model: str = "gpt-4o-mini") -> int:
    """Build a summary pool on disk: one .txt per trajectory."""
    from .trajectory import iter_trajectories, trajectory_text
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for traj in iter_trajectories(glob_pattern):
        text = trajectory_text(traj)
        if not text.strip():
            continue
        s = summarize_text(text, model=model, cache_dir=out_dir)
        name = (traj.instance_id or os.path.basename(traj.path)) + ".txt"
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(s)
        n += 1
        if n % 25 == 0:
            print(f"  summarized {n}")
    return n
