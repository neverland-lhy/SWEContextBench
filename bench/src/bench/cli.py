"""bench CLI: ingest / run / eval / matrix."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)

ROOT = Path(os.environ.get("SWECB_ROOT", Path(__file__).resolve().parents[3]))
BRIDGE_DIR = ROOT / "plugins" / "memory-bridge"

SETTINGS = ["no-context", "free-context", "free-summary"]
BACKENDS = ["trivial", "mem0", "custom"]


def _bridge(args: list[str]) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BRIDGE_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.call([sys.executable, "-m", "memory_bridge.cli", *args],
                           cwd=str(BRIDGE_DIR), env=env)


@app.command()
def ingest(
    setting: str = typer.Option("free-context", "--setting", "-s"),
    backend: str = typer.Option("trivial", "--backend", "-b"),
    backend_url: Optional[str] = typer.Option(None, "--backend-url"),
    store_path: Path = typer.Option(Path("memory_store.jsonl"), "--store-path"),
    subset: str = typer.Option("lite", "--subset"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Ingest 300 trajectories into a backend (free-context) or summaries
    (free-summary). no-context needs no ingest."""
    if setting == "no-context":
        print("no-context: nothing to ingest.")
        return
    from .tasks import past_experience_glob
    glob_pat = past_experience_glob(subset)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ingest", "--from", glob_pat, "--backend", backend,
            "--store-path", str(store_path)]
    if backend_url:
        args += ["--backend-url", backend_url]
    if setting == "free-summary":
        args += ["--summarize", "llm", "--summary-cache-dir", str(store_path.parent / "summaries")]
    if limit:
        args += ["--limit", str(limit)]
    print(f"ingesting {glob_pat} -> backend={backend} setting={setting}")
    raise typer.Exit(code=_bridge(args))


@app.command()
def run(
    setting: str = typer.Option("no-context", "--setting", "-s"),
    backend: str = typer.Option("trivial", "--backend", "-b"),
    backend_url: Optional[str] = typer.Option(None, "--backend-url"),
    agent: str = typer.Option("claude-code", "--agent", "-a"),
    model: str = typer.Option("glm-5.2", "--model", "-m"),
    subset: str = typer.Option("lite", "--subset"),
    tasks: Optional[str] = typer.Option(None, "--tasks", help="comma-sep instance_ids"),
    store_path: Path = typer.Option(Path("memory_store.jsonl"), "--store-path"),
    harbor: str = typer.Option("harbor", "--harbor"),
    work_dir: Path = typer.Option(Path("runs"), "--work-dir"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Run tasks through Harbor and collect patches into predictions/."""
    from .tasks import load_tasks
    from .harbor_gen import write_task_dir
    from .runner import run_task

    only = tasks.split(",") if tasks else None
    task_list = load_tasks(subset, only=only)
    if limit:
        task_list = task_list[:limit]
    print(f"{len(task_list)} tasks, setting={setting} backend={backend} agent={agent}")

    for i, t in enumerate(task_list):
        print(f"[{i+1}/{len(task_list)}] {t.instance_id}")
        task_dir = work_dir / f"{setting}_{backend}" / t.instance_id
        write_task_dir(task_dir, task=t, setting=setting, agent=agent,
                       model=model, backend=backend, backend_url=backend_url,
                       host_store_path=str(store_path.resolve()))
        # propagate store_path so the in-container bridge reads the same pool
        extra_env = {"MB_STORE_PATH": str(store_path)}
        run_task(t, task_dir=task_dir, harbor=harbor, agent=agent, model=model,
                 extra_env=extra_env)


@app.command()
def eval(
    run_id: str = typer.Argument(...),
    subset: str = typer.Option("lite", "--subset"),
) -> None:
    """Run SWEContextBench evaluation.sh on collected predictions."""
    from .runner import eval_run
    code = eval_run(run_id, subset)
    print(f"evaluation.sh exit={code}; report: {run_id}.json")
    raise typer.Exit(code=code)


@app.command()
def matrix(
    config: Path = typer.Option(Path("bench/matrix.yaml"), "--config"),
) -> None:
    """Run a matrix of {setting × backend × model} and eval each.

    matrix.yaml example:
        settings: [no-context, free-context, free-summary]
        backends: [trivial]
        models:
          glm-5.2: {agent: claude-code}
        subset: lite
    """
    import yaml
    cfg = yaml.safe_load(config.read_text())
    settings = cfg.get("settings", SETTINGS)
    backends = cfg.get("backends", ["trivial"])
    models = cfg.get("models", {"glm-5.2": {"agent": "claude-code"}})
    subset = cfg.get("subset", "lite")
    results: list[dict] = []

    for setting in settings:
        for backend in backends:
            for model, mc in models.items():
                agent = mc.get("agent", "claude-code")
                run_id = f"{setting}_{backend}_{model.split('/')[-1]}_{subset}"
                print(f"\n=== {run_id} ===")
                subprocess.call([sys.executable, "-m", "bench.cli", "ingest",
                                 "--setting", setting, "--backend", backend,
                                 "--subset", subset])
                rc = subprocess.call([sys.executable, "-m", "bench.cli", "run",
                                     "--setting", setting, "--backend", backend,
                                     "--agent", agent, "--model", model,
                                     "--subset", subset])
                if rc == 0:
                    subprocess.call([sys.executable, "-m", "bench.cli", "eval",
                                     run_id, "--subset", subset])
                    # read report
                    rep = ROOT / f"{run_id}.json"
                    if rep.exists():
                        import json
                        r = json.loads(rep.read_text())
                        results.append({"run_id": run_id, "setting": setting,
                                        "backend": backend, "model": model,
                                        "report": r})

    # summary table
    out = ROOT / "matrix_results.json"
    out.write_text(__import__("json").dumps(results, indent=2))
    print(f"\nmatrix results -> {out}")


if __name__ == "__main__":
    app()
