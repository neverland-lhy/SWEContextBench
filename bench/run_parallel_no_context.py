#!/usr/bin/env python3
"""Run no-context on ALL Lite tasks in parallel.

Each worker: build instance image (if missing) -> bench run (agent) ->
patch auto-collected -> rmi instance image (disk bound). Resumable: skips
tasks that already have a predictions/<id>_preds.json.
"""
from __future__ import annotations
import argparse, csv, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

ROOT = Path("/home/neverland/workspace/SWEContextBench")
os.environ["SWECB_ROOT"] = str(ROOT)  # bench.tasks.load_tasks reads this
BENCH_VENV_PYTHON = str(ROOT / "bench" / ".venv" / "bin" / "python")
LOG = Path("/tmp/run_parallel.log")
SUMMARY = Path("/tmp/run_parallel_summary.csv")
SUMMARY_LOCK = Lock()

PRED_DIR = ROOT / "predictions"
CASES = ROOT / "cases" / "SWEContextBench Lite"


def image_name(iid: str) -> str:
    return f"jiayuanz3/swecontextbench:{iid.replace('__', '.').lower()}"


def ensure_image(iid: str, img: str, pull_timeout: int) -> tuple[str, str]:
    """Return (status, img). Pull the pre-built image if missing. No build."""
    if subprocess.run(["docker", "images", "-q", img],
                     capture_output=True, text=True).stdout.strip():
        return "cached", img
    r = subprocess.run(["docker", "pull", "--platform", "linux/amd64", img],
                       capture_output=True, text=True, timeout=pull_timeout)
    if r.returncode == 0 and subprocess.run(
            ["docker", "images", "-q", img],
            capture_output=True, text=True).stdout.strip():
        return "pulled", img
    return "PULL_FAIL", img


def load_tasks() -> list[str]:
    sys.path.insert(0, str(ROOT / "bench" / "src"))
    from bench.tasks import load_tasks  # type: ignore
    return [t.instance_id for t in load_tasks("lite")]


def already_done(iid: str) -> bool:
    f = PRED_DIR / f"{iid}_preds.json"
    return f.exists() and f.stat().st_size > 50  # has a patch


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def run_one(iid: str, idx: int, total: int, pull_timeout: int,
            run_timeout: int, retries: int) -> dict:
    img = image_name(iid)
    bstat = pstat = "no"
    try:
        # 1) pull pre-built image if missing (with retry)
        for attempt in range(retries):
            bstat, _ = ensure_image(iid, img, pull_timeout)
            if bstat != "PULL_FAIL":
                break
            log(f"[{idx}/{total}] {iid}: pull retry {attempt+1}/{retries}")
        if bstat == "PULL_FAIL":
            log(f"[{idx}/{total}] {iid}: PULL FAIL ({iid})")
            return {"iid": iid, "build": bstat, "patch": pstat}

        # 2) run agent
        env = dict(os.environ)
        env["SWECB_ROOT"] = str(ROOT)
        cmd = [BENCH_VENV_PYTHON, "-m", "bench.cli", "run",
               "--setting", "no-context", "--tasks", iid, "--limit", "1"]
        try:
            r = subprocess.run(cmd, cwd=str(ROOT / "bench"), env=env,
                               capture_output=True, text=True,
                               timeout=run_timeout)
        except subprocess.TimeoutExpired:
            pstat = "RUN_TIMEOUT"
            log(f"[{idx}/{total}] {iid}: RUN TIMEOUT")
            return {"iid": iid, "build": bstat, "patch": pstat}

        if r.returncode != 0:
            pstat = "RUN_FAIL"
            log(f"[{idx}/{total}] {iid}: RUN FAIL ({r.stderr[-300:]})")
        elif already_done(iid):
            pstat = "yes"
            log(f"[{idx}/{total}] {iid}: ✓ patch collected")
        else:
            pstat = "no_patch"
            log(f"[{idx}/{total}] {iid}: no patch produced")
    except Exception as e:
        pstat = f"EXC:{e}"[:40]
        log(f"[{idx}/{total}] {iid}: EXC {e}")
    finally:
        # 3) free disk (base layers shared, kept)
        subprocess.run(["docker", "rmi", img],
                       capture_output=True, text=True)
    return {"iid": iid, "build": bstat, "patch": pstat}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--concurrency", type=int, default=5)
    ap.add_argument("--pull-timeout", type=int, default=600,
                    help="per-image docker pull timeout")
    ap.add_argument("--run-timeout", type=int, default=2400,
                    help="per-task agent run timeout")
    ap.add_argument("--retries", type=int, default=2,
                    help="pull retries on failure")
    a = ap.parse_args()

    LOG.write_text("")  # reset
    with SUMMARY.open("w", newline="") as f:
        csv.writer(f).writerow(["instance_id", "image", "patch"])

    all_ids = load_tasks()
    # resume: skip done ones
    todo = [i for i in all_ids if not already_done(i)]
    skipped = len(all_ids) - len(todo)
    log(f"total={len(all_ids)} already_done={skipped} todo={len(todo)} "
        f"concurrency={a.concurrency}")

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(run_one, iid, i + 1, len(todo),
                          a.pull_timeout, a.run_timeout, a.retries): iid
                for i, iid in enumerate(todo)}
        for fut in as_completed(futs):
            res = fut.result()
            with SUMMARY_LOCK, SUMMARY.open("a", newline="") as f:
                csv.writer(f).writerow(
                    [res["iid"], res["build"], res["patch"]])

    log("=== ALL DONE ===")
    # tally
    rows = list(csv.DictReader(SUMMARY.open()))
    patch_ok = sum(1 for r in rows if r["patch"] == "yes")
    log(f"patched={patch_ok}/{len(rows)}  (see {SUMMARY})")


if __name__ == "__main__":
    main()
