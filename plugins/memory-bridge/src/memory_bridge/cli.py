"""memory-bridge CLI: serve-mcp / add / search / ingest / summarize."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Optional

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)

# shared option defaults
BACKEND = typer.Option("trivial", "--backend", "-b", help="trivial|mem0|custom")
BACKEND_URL = typer.Option(None, "--backend-url",
                           help="custom AML HTTP url, or mem0 base url")
STORE_PATH = typer.Option(None, "--store-path",
                          help="trivial backend JSONL store path")


def _kwargs(backend: str, backend_url: Optional[str],
            store_path: Optional[str]) -> dict:
    kw: dict = {}
    if backend == "custom":
        if not backend_url:
            raise typer.BadParameter("--backend-url required for --backend custom")
        kw["backend_url"] = backend_url
    if backend == "mem0" and backend_url:
        kw["base_url"] = backend_url
    if backend == "trivial" and store_path:
        kw["store_path"] = store_path
    return kw


@app.command()
def serve_mcp(
    backend: str = BACKEND,
    backend_url: Optional[str] = BACKEND_URL,
    store_path: Optional[str] = STORE_PATH,
) -> None:
    """Run the MCP server over stdio (Claude Code / Harbor spawns this)."""
    from .mcp_server import run_stdio
    run_stdio(backend, **_kwargs(backend, backend_url, store_path))


@app.command()
def add(
    content: Optional[str] = typer.Option(None, "--content"),
    messages_file: Optional[str] = typer.Option(
        None, "--messages", help="JSON file: list of {role,content}"),
    user_id: str = typer.Option("default", "--user-id"),
    session_id: str = typer.Option("cli", "--session-id"),
    backend: str = BACKEND,
    backend_url: Optional[str] = BACKEND_URL,
    store_path: Optional[str] = STORE_PATH,
) -> None:
    """Add memory (single op, for hook scripts)."""
    from .protocol import get_backend
    if messages_file:
        msgs = json.load(open(messages_file))
    elif content:
        msgs = [{"role": "user", "content": content}]
    else:
        msgs = [{"role": "user", "content": sys.stdin.read()}]
    b = get_backend(backend, **_kwargs(backend, backend_url, store_path))
    res = b.add(request_id=str(uuid.uuid4()), messages=msgs,
                user_id=user_id, session_id=session_id)
    print(json.dumps(res, ensure_ascii=False))


@app.command()
def search(
    query: str = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    top_k: int = typer.Option(5, "--top-k"),
    backend: str = BACKEND,
    backend_url: Optional[str] = BACKEND_URL,
    store_path: Optional[str] = STORE_PATH,
    raw: bool = typer.Option(False, "--raw", help="print content only"),
) -> None:
    """Search memories (single op, for hook scripts)."""
    from .protocol import get_backend
    b = get_backend(backend, **_kwargs(backend, backend_url, store_path))
    res = b.search(query=query, user_id=user_id, top_k=top_k)
    if raw:
        print("\n---\n".join(m.content for m in res))
    else:
        print(json.dumps(
            [{"id": m.id, "score": m.score, "content": m.content[:300]}
             for m in res],
            ensure_ascii=False, indent=2))


@app.command()
def ingest(
    frm: str = typer.Option(..., "--from", help="glob for .jsonl trajectories"),
    backend: str = BACKEND,
    backend_url: Optional[str] = BACKEND_URL,
    store_path: Optional[str] = typer.Option(
        "memory_store.jsonl", "--store-path"),
    summarize: Optional[str] = typer.Option(
        None, "--summarize", help="llm | field | (none: full trajectory)"),
    summary_cache_dir: Optional[str] = typer.Option(
        "cache/summaries", "--summary-cache-dir"),
    llm_model: str = typer.Option("gpt-4o-mini", "--llm-model"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Bulk-ingest SWE-ContextBench trajectories into a backend."""
    from .ingest import ingest as _ingest
    res = _ingest(
        glob_pattern=frm, backend=backend, summarize=summarize,
        backend_url=backend_url, store_path=store_path,
        limit=limit, summary_cache_dir=summary_cache_dir, llm_model=llm_model,
    )
    print(json.dumps(res, ensure_ascii=False))


@app.command()
def summarize(
    frm: str = typer.Option(..., "--from", help="glob for .jsonl trajectories"),
    out_dir: str = typer.Option("cache/summaries", "--out"),
    llm_model: str = typer.Option("gpt-4o-mini", "--llm-model"),
) -> None:
    """Build a summary pool on disk (one .txt per trajectory)."""
    from .summarize import summarize_glob
    n = summarize_glob(frm, out_dir=out_dir, model=llm_model)
    print(json.dumps({"summarized": n, "out_dir": out_dir}))


if __name__ == "__main__":
    app()
