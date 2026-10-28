#!/usr/bin/env python3
"""Unified experiment runner for SWEContextBench — workbuddy-bench style.

A single config-driven entry point that runs one (setting × backend × model)
experiment over the task set: ingests memory (if Free), runs the agent in
parallel over all tasks (capturing patches + token usage from harbor's own
trajectory.json), then batch-evaluates with evaluation.sh and prints a full
report (resolve rate, per-repo breakdown, token + cost totals).

Design borrowed from workbuddy-bench:
  - YAML run profile as single source of truth (+ CLI overrides)
  - `harbor run -c <job.yaml> --path <task_dir>` per task (job.yaml carries the
    agent kwargs: MCP server, hooks, permissions; task.toml carries the
    prebuilt instance image + verifier)
  - sharded parallelism via ThreadPoolExecutor (per-task image pull/cleanup +
    resumability, which harbor's own -n-concurrent can't give us for prebuilt
    per-instance images)
  - token accounting parsed from harbor's trajectory.json final_metrics
    (no proxy needed — the stock claude-code agent already emits it)

Usage:
  bench/.venv/bin/python run.py                         # default profile
  bench/.venv/bin/python run.py --setting free-context   # CLI override
  bench/.venv/bin/python run.py --profile bench/profiles/free-context.yaml
  bench/.venv/bin/python run.py -n 8 --limit 5 --no-eval  # smoke

Run with the bench venv so `bench.*` and `memory_bridge` import cleanly.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ── path bootstrap: make bench.* + memory_bridge importable ──────────────
ROOT = Path(__file__).resolve().parent
BENCH_SRC = ROOT / "bench" / "src"
BRIDGE_SRC = ROOT / "plugins" / "memory-bridge" / "src"
for p in (str(BENCH_SRC), str(BRIDGE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("SWECB_ROOT", str(ROOT))

from bench.tasks import load_tasks, past_experience_glob, Task  # noqa: E402
from bench.harbor_gen import write_task_dir, instance_image_name  # noqa: E402
from bench.runner import _anthropic_env  # noqa: E402

PREDICTIONS_DIR = ROOT / "predictions"
DEFAULT_PROFILE = ROOT / "bench" / "profiles" / "no-context.yaml"
TICKER_INTERVAL = 5.0  # seconds between live status lines


def predictions_dir(run_id: str) -> Path:
    """Per-run predictions subdir. Scope by run_id so a No-Context run's
    patches don't make a later Free run skip every task (resume check)."""
    d = PREDICTIONS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── pretty printers ──────────────────────────────────────────────────────
def fmt_tok(n: int | float | None) -> str:
    if not n:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def fmt_cost(c: float | None) -> str:
    if not c:
        return "$0.00"
    return f"${c:.2f}"


def fmt_dur(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s//3600}h{(s%3600)//60}m"
    if s >= 60:
        return f"{s//60}m{s%60}s"
    return f"{s}s"


def now() -> str:
    return time.strftime("%H:%M:%S")


# ── per-task result ──────────────────────────────────────────────────────
@dataclass
class TaskResult:
    instance_id: str
    status: str = "pending"        # pulled/cached, patch, no_patch, run_fail, timeout, exc, pull_fail
    patch: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    n_steps: int = 0
    duration_s: float = 0.0
    resolved: bool | None = None   # filled after eval
    fail_to_pass: int = 0
    fail_to_pass_ok: int = 0
    error: str = ""


# ── docker image helpers (ported from run_parallel_no_context.py) ────────
def image_name(iid: str) -> str:
    return instance_image_name(iid)


def ensure_image(iid: str, img: str, pull_timeout: int) -> str:
    """Pull the prebuilt instance image if missing. Returns status string."""
    if subprocess.run(["docker", "images", "-q", img],
                      capture_output=True, text=True).stdout.strip():
        return "cached"
    r = subprocess.run(["docker", "pull", "--platform", "linux/amd64", img],
                       capture_output=True, text=True, timeout=pull_timeout)
    if r.returncode == 0 and subprocess.run(
            ["docker", "images", "-q", img],
            capture_output=True, text=True).stdout.strip():
        return "pulled"
    return "pull_fail"


# ── trajectory / token extraction ────────────────────────────────────────
def extract_trial_metrics(jobs_dir: Path, instance_id: str,
                          after: float = 0.0) -> dict:
    """Find the most recent trajectory.json for this instance under jobs_dir
    and pull final_metrics. Harbor writes <jobs>/<ts>/<trial>/agent/trajectory.json
    where trial_name starts with the instance id."""
    cands = sorted(
        jobs_dir.glob(f"**/{instance_id}__*/agent/trajectory.json"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not cands:
        cands = sorted(
            jobs_dir.glob(f"**/{instance_id}*/agent/trajectory.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    for p in cands:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        fm = d.get("final_metrics") or {}
        steps = d.get("steps") or []
        return {
            "input_tokens": int(fm.get("total_prompt_tokens") or 0),
            "output_tokens": int(fm.get("total_completion_tokens") or 0),
            "cached_tokens": int(fm.get("total_cached_tokens") or 0),
            "cost_usd": float(fm.get("total_cost_usd") or 0.0),
            "n_steps": len(steps),
            "traj_path": str(p),
            "fresh": p.stat().st_mtime >= after - 1.0,
        }
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "cost_usd": 0.0, "n_steps": 0, "traj_path": "", "fresh": False}


def extract_patch(jobs_dir: Path, instance_id: str) -> str | None:
    """Find the verifier/model.patch the capture-script wrote for this trial.
    Excludes test-file hunks (gold test_patch supplies the tests)."""
    import re
    cands = sorted(
        jobs_dir.glob(f"**/{instance_id}__*/verifier/model.patch"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in cands:
        try:
            txt = p.read_text()
        except Exception:
            continue
        if txt.strip():
            # strip test-file hunks so they don't collide with gold test_patch
            parts = re.split(r"(?=^diff --git )", txt, flags=re.M)
            return "".join(
                q for q in parts
                if q.strip() and not re.match(r"diff --git a/tests?/", q)
            )
    return None


# ── ingest (Free settings only) ──────────────────────────────────────────
def run_ingest(cfg: dict) -> int:
    """Call memory-bridge ingest to populate the memory pool once."""
    setting = cfg["setting"]
    if setting == "no-context":
        print(f"[ingest] {setting}: nothing to ingest")
        return 0
    glob_pat = past_experience_glob(cfg["subset"])
    store = Path(cfg["store_path"]).resolve()
    store.parent.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable, "-m", "memory_bridge.cli", "ingest",
        "--from", glob_pat,
        "--backend", cfg["backend"],
        "--store-path", str(store),
    ]
    if cfg.get("backend_url"):
        args += ["--backend-url", cfg["backend_url"]]
    if setting == "free-summary":
        args += ["--summarize", "llm",
                 "--summary-cache-dir", str(store.parent / "summaries")]
    if cfg.get("limit"):
        args += ["--limit", str(cfg["limit"])]
    env = dict(os.environ)
    print(f"[ingest] {glob_pat} -> backend={cfg['backend']} setting={setting}")
    r = subprocess.run(args, env=env)
    return r.returncode


# ── the per-task worker ───────────────────────────────────────────────────
def run_one(task: Task, cfg: dict, jobs_dir: Path, idx: int, total: int,
            prog: "Progress") -> TaskResult:
    iid = task.instance_id
    res = TaskResult(instance_id=iid)
    t0 = time.time()
    prog.start(iid, idx)  # harbor.log path registered below once task_dir known
    try:
        # 1) ensure prebuilt image (with retry)
        img = image_name(iid)
        bstat = "pull_fail"
        for attempt in range(cfg["retries"]):
            bstat = ensure_image(iid, img, cfg["pull_timeout"])
            if bstat != "pull_fail":
                break
        if bstat == "pull_fail":
            res.status = "pull_fail"
            res.error = "image pull failed"
            prog.finish(iid, res)
            return res

        # 2) generate task dir + job.yaml + task.toml (run-scoped, beside jobs/)
        setting = cfg["setting"]
        task_dir = jobs_dir.parent / "tasks" / iid
        write_task_dir(task_dir, task=task, setting=setting,
                       agent=cfg["agent"], model=cfg["model"],
                       backend=cfg["backend"],
                       backend_url=cfg.get("backend_url"),
                       host_store_path=cfg["store_path"])
        harbor_log = task_dir / "harbor.log"
        prog.set_log(iid, harbor_log)

        # 3) .env with anthropic routing (token stays out of process args)
        anth_env = _anthropic_env()
        env_file = task_dir / ".env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in anth_env.items()) + "\n")

        # 4) harbor run — pass BOTH -p (task.toml: image+verifier) and
        #    -c job.yaml (agent kwargs: MCP/hooks/permissions). The old runner
        #    omitted -c, so Free mode silently lost its MCP/hooks.
        #    Use a FRESH per-task jobs dir (wiped first) so harbor can't resume
        #    a stale trial from a prior run and bail without executing — that
        #    previously made us report last run's trajectory/tokens as this
        #    run's result. Extraction is scoped to this dir for the same reason.
        import shutil
        task_jobs = jobs_dir / iid
        shutil.rmtree(task_jobs, ignore_errors=True)
        task_jobs.mkdir(parents=True, exist_ok=True)
        store = Path(cfg["store_path"]).resolve()
        mb_env = {"MB_STORE_PATH": str(store),
                  "MB_BACKEND": cfg["backend"],
                  "MB_BACKEND_URL": cfg.get("backend_url") or "",
                  "MB_USER_ID": task.user_id}
        cmd = [
            cfg["harbor"], "run",
            "-p", str(task_dir),
            "-c", str(task_dir / "job.yaml"),
            "-a", cfg["agent"],
            "-m", cfg["model"],
            "--env-file", str(env_file),
            "-o", str(task_jobs),
            "--timeout-multiplier", "2",
        ]
        run_env = dict(os.environ)
        run_env.update(mb_env)
        # Stream harbor output to a per-task log (NOT --quiet, NOT captured) so
        # the ticker can tail it for live signal and Ctrl+C can kill the proc.
        harbor_log_fp = open(harbor_log, "w", buffering=1)
        harbor_rc = None
        try:
            proc = subprocess.Popen(cmd, env=run_env, stdout=harbor_log_fp,
                                    stderr=subprocess.STDOUT, text=True)
            prog.register_proc(iid, proc)
            try:
                harbor_rc = proc.wait(timeout=cfg["run_timeout"])
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                res.status = "timeout"
                res.error = "agent run timed out"
                prog.finish(iid, res)
                return res
            finally:
                prog.unregister_proc(iid)
        finally:
            harbor_log_fp.close()

        # 5) extract patch + tokens from THIS invocation's trial artifacts only.
        m = extract_trial_metrics(task_jobs, iid, after=t0)
        # if harbor bailed without producing a fresh trajectory (e.g. lock
        # mismatch / config error), don't report stale zeros as a real result.
        if m["traj_path"] and m["fresh"]:
            res.input_tokens = m["input_tokens"]
            res.output_tokens = m["output_tokens"]
            res.cached_tokens = m["cached_tokens"]
            res.cost_usd = m["cost_usd"]
            res.n_steps = m["n_steps"]
        elif m["traj_path"] and not m["fresh"]:
            res.status = "stale"
            res.error = "harbor reused a stale trial (no new trajectory)"
            prog.finish(iid, res)
            return res

        patch = extract_patch(task_jobs, iid)
        res.patch = bool(patch and patch.strip())
        if res.patch:
            res.status = "patch"
            pdir = predictions_dir(cfg["run_id"])
            (pdir / f"{iid}_preds.json").write_text(json.dumps({
                iid: {
                    "model_name_or_path": cfg["model"],
                    "instance_id": iid,
                    "model_patch": patch,
                }
            }, ensure_ascii=False, indent=2))
        else:
            res.status = "no_patch"
            res.error = "no diff captured"
    except Exception as e:
        res.status = "exc"
        res.error = str(e)[:120]
    finally:
        res.duration_s = time.time() - t0
        # free disk: instance image layers are shared with eval; drop the tag
        subprocess.run(["docker", "rmi", image_name(iid)],
                       capture_output=True, text=True)
        prog.finish(iid, res)
    return res


# ── live progress tracker ────────────────────────────────────────────────
def _tail(path: Path, n: int = 60) -> str:
    """Last non-empty line of a file, truncated — gives the agent's latest
    action for the ticker without needing harbor's structured progress."""
    try:
        data = path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    for line in reversed(data):
        line = line.strip()
        # skip ANSI-rich harbor chrome / blank lines
        if line and not line.startswith("\x1b"):
            return line[:n]
    return ""


class Progress:
    def __init__(self, total: int, log_path: Path):
        self.total = total
        self.lock = threading.Lock()
        self.done = 0
        self.in_flight: dict[str, tuple[int, float]] = {}  # iid -> (idx, start_t)
        self.logs: dict[str, Path] = {}        # iid -> per-task harbor.log
        self.results: list[TaskResult] = []
        self.t_in = self.t_out = self.t_cache = 0
        self.t_cost = 0.0
        self.t0 = time.time()
        self.log_fp = open(log_path, "a", buffering=1)  # line-buffered
        self._stop = False
        # terminal write serialization: the live \r line (ticker) and the \n
        # completion lines (worker threads) must not interleave.
        self.term_lock = threading.Lock()
        self._live_drawn = False  # whether a \r line is currently on screen
        # process registry so Ctrl+C can kill running harbor subprocesses
        self._procs: dict[str, subprocess.Popen] = {}
        self._plock = threading.Lock()

    def start(self, iid: str, idx: int, log_path: Path | None = None) -> None:
        with self.lock:
            self.in_flight[iid] = (idx, time.time())
            if log_path:
                self.logs[iid] = log_path
        self._redraw_live()

    def set_log(self, iid: str, log_path: Path) -> None:
        with self.lock:
            self.logs[iid] = log_path
        self._redraw_live()

    def register_proc(self, iid: str, proc: subprocess.Popen) -> None:
        with self._plock:
            self._procs[iid] = proc

    def unregister_proc(self, iid: str) -> None:
        with self._plock:
            self._procs.pop(iid, None)

    def kill_all(self) -> None:
        """Terminate every running harbor subprocess (Ctrl+C). Non-blocking —
        safe to call from a signal handler (no sleep)."""
        with self._plock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    def _clear_live(self) -> None:
        """Erase the in-place live line so a \n event line can print cleanly."""
        if self._live_drawn:
            sys.stdout.write("\r\x1b[K")  # CR + erase-to-end-of-line
            self._live_drawn = False

    def _draw_live(self, line: str) -> None:
        """Redraw the single live status line in place (no scroll)."""
        with self.term_lock:
            sys.stdout.write("\r\x1b[K" + line)
            sys.stdout.flush()
            self._live_drawn = True

    def _print_event(self, line: str) -> None:
        """Print a permanent \n line (task completion). Clears the live line
        first so it doesn't get half-overwritten."""
        with self.term_lock:
            self._clear_live()
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            self.log_fp.write(line + "\n")

    def finish(self, iid: str, res: TaskResult) -> None:
        with self.lock:
            self.in_flight.pop(iid, None)
            self.logs.pop(iid, None)
            self.done += 1
            self.results.append(res)
            self.t_in += res.input_tokens
            self.t_out += res.output_tokens
            self.t_cache += res.cached_tokens
            self.t_cost += res.cost_usd
            mark = {"patch": "✓", "no_patch": "·", "timeout": "⏱",
                    "run_fail": "✗", "exc": "!", "pull_fail": "⊘"}.get(res.status, "?")
            line = (f"[{now()}] {mark} {iid}  "
                    f"{res.status}  {res.n_steps}t  "
                    f"in {fmt_tok(res.input_tokens)} (cache {fmt_tok(res.cached_tokens)}) "
                    f"· out {fmt_tok(res.output_tokens)} · {fmt_cost(res.cost_usd)} "
                    f"· {fmt_dur(res.duration_s)}"
                    f"{'  '+res.error if res.error else ''}")
        self._print_event(line)
        # redraw the live line immediately so it doesn't vanish after an event
        self._redraw_live()

    def _build_live(self) -> str:
        now_t = time.time()
        with self.lock:
            elapsed = now_t - self.t0
            running_iids = list(self.in_flight.items())
            tails = {iid: _tail(self.logs[iid]) for iid, _ in running_iids
                     if iid in self.logs}
        parts = []
        for iid, (idx, st) in running_iids[:6]:
            age = now_t - st
            t = tails.get(iid, "")
            parts.append(f"{iid}({fmt_dur(age)})" + (f" {t}" if t else ""))
        return (f"[{now()}] ▶ {self.done}/{self.total} done  "
                f"running({len(running_iids)}): {' | '.join(parts) or '-'}  "
                f"tok in {fmt_tok(self.t_in)} out {fmt_tok(self.t_out)} "
                f"(cache {fmt_tok(self.t_cache)}) · {fmt_cost(self.t_cost)}  "
                f"elapsed {fmt_dur(elapsed)}")

    def _redraw_live(self) -> None:
        if self._stop:
            return
        self._draw_live(self._build_live())

    def ticker(self) -> None:
        # draw immediately, then refresh the SAME line every tick (no scroll).
        self._redraw_live()
        while not self._stop:
            time.sleep(TICKER_INTERVAL)
            self._redraw_live()

    def stop(self) -> None:
        self._stop = True
        # move past the live line so the next output isn't on the same line
        with self.term_lock:
            if self._live_drawn:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._live_drawn = False
        self.log_fp.close()


# ── evaluation phase ─────────────────────────────────────────────────────
def run_eval(cfg: dict, run_id: str) -> dict:
    """Call evaluation.sh; parse <run_id>.json report into per-instance resolved.
    evaluation.sh signature: eval.sh <run_id> <subset> [predictions_dir]. We pass
    the per-run predictions subdir so concurrent/sequential runs don't collide."""
    script = ROOT / "evaluation.sh"
    if not script.exists():
        print(f"[eval] {script} not found; skipping")
        return {}
    pdir = predictions_dir(run_id)
    print(f"\n[eval] running evaluation.sh {run_id} {cfg['subset']} {pdir}")
    rc = subprocess.call(["bash", str(script), run_id, cfg["subset"], str(pdir)])
    report = ROOT / f"{run_id}.json"
    if not report.exists():
        print(f"[eval] no report at {report} (rc={rc})")
        return {}
    return json.loads(report.read_text())


def merge_eval(results: list[TaskResult], report: dict) -> None:
    for r in results:
        inst = report.get(r.instance_id)
        if not inst:
            continue
        r.resolved = bool(inst.get("resolved"))
        ftp = (inst.get("tests_status", {}) or {}).get("FAIL_TO_PASS", {}) or {}
        f2p = ftp.get("success", []) or []
        r.fail_to_pass = len(f2p) + len(ftp.get("failure", []) or [])
        r.fail_to_pass_ok = len(f2p)


# ── reporting ────────────────────────────────────────────────────────────
def write_report(cfg: dict, results: list[TaskResult], report: dict,
                 run_id: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    t_in = sum(r.input_tokens for r in results)
    t_out = sum(r.output_tokens for r in results)
    t_cache = sum(r.cached_tokens for r in results)
    t_cost = sum(r.cost_usd for r in results)
    patched = [r for r in results if r.patch]
    resolved = [r for r in results if r.resolved]
    wall = sum(r.duration_s for r in results)

    # CSV
    with (out_dir / "results.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instance_id", "status", "patch", "resolved",
                    "input_tok", "output_tok", "cached_tok", "cost_usd",
                    "steps", "duration_s", "ftp_ok", "ftp_total", "error"])
        for r in sorted(results, key=lambda x: x.instance_id):
            w.writerow([r.instance_id, r.status, int(r.patch),
                         "" if r.resolved is None else int(r.resolved),
                         r.input_tokens, r.output_tokens, r.cached_tokens,
                         f"{r.cost_usd:.4f}", r.n_steps, f"{r.duration_s:.0f}",
                         r.fail_to_pass_ok, r.fail_to_pass, r.error])

    # per-repo breakdown
    by_repo: dict[str, list[TaskResult]] = {}
    for r in results:
        repo = r.instance_id.split("__", 1)[0]
        by_repo.setdefault(repo, []).append(r)
    repo_rows = []
    for repo in sorted(by_repo):
        rs = by_repo[repo]
        n_res = sum(1 for r in rs if r.resolved)
        n_patch = sum(1 for r in rs if r.patch)
        repo_rows.append({
            "repo": repo, "total": len(rs), "patched": n_patch,
            "resolved": n_res,
            "tokens_in": sum(r.input_tokens for r in rs),
            "tokens_out": sum(r.output_tokens for r in rs),
            "cost": round(sum(r.cost_usd for r in rs), 2),
        })

    summary = {
        "run_id": run_id, "setting": cfg["setting"], "backend": cfg["backend"],
        "model": cfg["model"], "subset": cfg["subset"],
        "total_tasks": len(results),
        "patched": len(patched), "resolved": len(resolved),
        "resolve_rate": round(len(resolved) / max(1, len(results)), 4),
        "tokens": {"input": t_in, "output": t_out, "cached": t_cache,
                   "total": t_in + t_out},
        "cost_usd": round(t_cost, 4),
        "wall_time_s": round(wall, 0),
        "concurrency": cfg["concurrency"],
        "per_repo": repo_rows,
        "resolved_ids": [r.instance_id for r in resolved],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))

    # console table
    print("\n" + "=" * 70)
    print(f"  {run_id}  —  resolve {len(resolved)}/{len(results)} "
          f"({100*len(resolved)/max(1,len(results)):.1f}%)  "
          f"patched {len(patched)}")
    print("-" * 70)
    print(f"  {'repo':<16} {'total':>5} {'patch':>6} {'resolv':>6} "
          f"{'tok_in':>9} {'tok_out':>8} {'cost':>7}")
    for row in repo_rows:
        print(f"  {row['repo']:<16} {row['total']:>5} {row['patched']:>6} "
              f"{row['resolved']:>6} {fmt_tok(row['tokens_in']):>9} "
              f"{fmt_tok(row['tokens_out']):>8} {fmt_cost(row['cost']):>7}")
    print("-" * 70)
    print(f"  {'TOTAL':<16} {len(results):>5} {len(patched):>6} "
          f"{len(resolved):>6} {fmt_tok(t_in):>9} {fmt_tok(t_out):>8} "
          f"{fmt_cost(t_cost):>7}")
    print(f"  cached tokens: {fmt_tok(t_cache)}   "
          f"wall (sum of task dur): {fmt_dur(wall)}   "
          f"concurrency: {cfg['concurrency']}")
    print(f"  summary -> {out_dir / 'summary.json'}")
    print(f"  csv     -> {out_dir / 'results.csv'}")
    if report:
        print(f"  swebench report -> {run_id}.json")
    print("=" * 70)


# ── config ───────────────────────────────────────────────────────────────
DEFAULTS = dict(
    setting="no-context", backend="trivial", backend_url=None,
    model="glm-5.2", agent="claude-code", subset="lite",
    concurrency=5, pull_timeout=1800, run_timeout=2400, retries=2,
    limit=None, tasks=None, store_path="memory_store.jsonl",
    work_dir="runs", harbor="harbor", ingest=True, eval=True,
)


def load_profile(path: Path | None) -> dict:
    cfg = dict(DEFAULTS)
    if path and path.exists():
        import yaml
        cfg.update(yaml.safe_load(path.read_text()) or {})
    return cfg


def resolve_run_base(cfg: dict) -> str:
    """Semantic base id: <setting>_<backend>_<model>_<subset>. Stable across
    re-runs of the same config — used as the prefix to find a run to resume."""
    mid = cfg["model"].split("/")[-1]
    return f"{cfg['setting']}_{cfg['backend']}_{mid}_{cfg['subset']}"


def _stamp() -> str:
    """Compact timestamp + short random suffix → unique even for same-second
    re-runs, so multiple runs of the same config never overwrite each other."""
    import secrets
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def latest_run_id(base: str, work_root: Path) -> str | None:
    """Most recent existing run_id matching the semantic base (for --continue)."""
    if not work_root.exists():
        return None
    cands = sorted(p.name for p in work_root.glob(f"{base}__*")
                   if p.is_dir() and "__" in p.name)
    return cands[-1] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(description="SWEContextBench experiment runner")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                    help="YAML run profile (default: bench/profiles/no-context.yaml)")
    ap.add_argument("--setting", choices=["no-context", "free-context", "free-summary"])
    ap.add_argument("--backend", choices=["trivial", "mem0", "custom"])
    ap.add_argument("--backend-url")
    ap.add_argument("--model")
    ap.add_argument("--agent")
    ap.add_argument("--subset")
    ap.add_argument("-n", "--concurrency", type=int)
    ap.add_argument("--pull-timeout", type=int)
    ap.add_argument("--run-timeout", type=int)
    ap.add_argument("--retries", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tasks", help="comma-sep instance_ids (overrides subset)")
    ap.add_argument("--store-path")
    ap.add_argument("--work-dir")
    ap.add_argument("--harbor")
    ap.add_argument("--no-ingest", action="store_true")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--run-id",
                    help="use this exact run_id (resume that run if it exists, "
                         "or start a named run). Default: base + timestamp.")
    ap.add_argument("--continue", dest="continue_run", action="store_true",
                    help="reuse the most recent existing run matching this "
                         "config (auto-resume without typing the timestamp). "
                         "Overrides the default new-timestamp behavior.")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="skip tasks that already have a predictions patch "
                         "(scoped to the resolved run_id)")
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    args = ap.parse_args()

    cfg = load_profile(args.profile)
    # CLI overrides
    for k in ("setting", "backend", "backend_url", "model", "agent", "subset",
              "concurrency", "pull_timeout", "run_timeout", "retries", "limit",
              "tasks", "store_path", "work_dir", "harbor"):
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    if args.no_ingest:
        cfg["ingest"] = False
    if args.no_eval:
        cfg["eval"] = False
    cfg["resume"] = args.resume  # CLI flag (default True); not in the loop above

    # run_id resolution: each invocation is unique by default (base + timestamp)
    # so re-running the same config never overwrites — results accumulate under
    # runs/<run_id>/ and predictions/<run_id>/. To resume a specific run instead,
    # pass --run-id <id> or --continue (reuses the latest matching run).
    base = resolve_run_base(cfg)
    work_root = ROOT / cfg["work_dir"]
    if args.run_id:
        run_id = args.run_id
    elif args.continue_run:
        prev = latest_run_id(base, work_root)
        if prev:
            run_id = prev
            print(f"[continue] resuming existing run: {run_id}")
        else:
            run_id = f"{base}__{_stamp()}"
            print(f"[continue] no prior run found; starting new: {run_id}")
    else:
        run_id = f"{base}__{_stamp()}"
    cfg["run_id"] = run_id  # so run_one can find the per-run predictions dir
    work_dir = work_root / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = work_dir / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    log_path = work_dir / "run.log"
    cfg["store_path"] = str((ROOT / cfg["store_path"]).resolve())

    # task list
    only = cfg["tasks"].split(",") if cfg.get("tasks") else None
    task_list = load_tasks(cfg["subset"], only=only)
    if cfg.get("limit"):
        task_list = task_list[:cfg["limit"]]

    # resumability: drop tasks that already produced a patch (run-scoped)
    pdir = predictions_dir(run_id)
    if cfg.get("resume"):
        before = len(task_list)
        task_list = [t for t in task_list
                     if not (pdir / f"{t.instance_id}_preds.json").exists()]
        skipped = before - len(task_list)
    else:
        skipped = 0

    # header
    print("=" * 70)
    print(f"  run_id: {run_id}")
    print(f"  setting={cfg['setting']}  backend={cfg['backend']}  "
          f"model={cfg['model']}  agent={cfg['agent']}  subset={cfg['subset']}")
    print(f"  tasks: {len(task_list)} ({skipped} resumed/skipped)  "
          f"concurrency={cfg['concurrency']}")
    print(f"  work_dir: {work_dir}")
    print(f"  store: {cfg['store_path']}  ingest={cfg['ingest']}  eval={cfg['eval']}")
    print("=" * 70)

    if not task_list:
        print("nothing to run (all tasks already have predictions for this run_id).")
        if cfg.get("eval"):
            report = run_eval(cfg, run_id)
            # rebuild results from existing predictions for the report table
            results: list[TaskResult] = []
            for p in sorted(pdir.glob("*_preds.json")):
                iid = p.stem.replace("_preds", "")
                results.append(TaskResult(instance_id=iid, status="patch",
                                           patch=True))
            merge_eval(results, report)
            write_report(cfg, results, report, run_id, work_dir)
        return 0

    # 1) ingest (Free only)
    if cfg.get("ingest") and cfg["setting"] != "no-context":
        rc = run_ingest(cfg)
        if rc != 0:
            print(f"[ingest] failed (rc={rc}); aborting")
            return rc

    # 2) parallel agent runs
    prog = Progress(len(task_list), log_path)
    ticker = threading.Thread(target=prog.ticker, daemon=True)
    ticker.start()
    # Ctrl+C must kill running harbor subprocesses and cancel pending tasks.
    # A custom SIGINT handler kills procs + sets stop_event; the main loop polls
    # stop_event every 0.5s (via futures.wait) so it notices the interrupt
    # promptly instead of blocking in as_completed.
    import signal
    from concurrent.futures import wait, FIRST_COMPLETED
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        if not stop_event.is_set():
            print(f"\n[interrupt] signal {signum}: killing harbor subprocesses…",
                  flush=True)
        stop_event.set()
        prog.kill_all()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)

    ex = ThreadPoolExecutor(max_workers=cfg["concurrency"])
    futs = {ex.submit(run_one, t, cfg, jobs_dir, i + 1, len(task_list), prog): t
            for i, t in enumerate(task_list)}
    results: list[TaskResult] = []
    pending = set(futs)
    try:
        while pending and not stop_event.is_set():
            done, pending = wait(pending, timeout=0.5,
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    results.append(fut.result())
                except Exception as e:
                    t = futs[fut]
                    results.append(TaskResult(instance_id=t.instance_id,
                                              status="exc", error=str(e)[:120]))
    finally:
        if stop_event.is_set():
            for f in pending:
                f.cancel()
            prog.kill_all()
        ex.shutdown(wait=False, cancel_futures=True)
    prog.stop()
    ticker.join(timeout=2)
    if stop_event.is_set():
        write_report(cfg, results, {}, run_id, work_dir)
        print("[interrupt] partial results written; exiting.")
        return 130

    # 3) eval
    report: dict = {}
    if cfg.get("eval"):
        report = run_eval(cfg, run_id)
        if report:
            merge_eval(results, report)

    # 4) report
    write_report(cfg, results, report, run_id, work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
