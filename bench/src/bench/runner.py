"""Run one task through Harbor and collect the model patch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .tasks import Task

PREDICTIONS_DIR = Path(
    os.environ.get("SWECB_ROOT", os.getcwd())) / "predictions"

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _anthropic_env() -> dict:
    """Load ANTHROPIC_* env from the local Claude settings.json so harbor's
    claude-code agent routes to the user's endpoint (e.g. DashScope + glm-5.2)."""
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        import json
        env = json.loads(CLAUDE_SETTINGS.read_text()).get("env", {})
        # only pass through the routing-relevant vars
        keep = {k: v for k, v in env.items()
                if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_")}
        return keep
    except Exception:
        return {}


def _run(cmd: list[str], cwd: str | None = None,
         env: dict | None = None) -> tuple[int, str]:
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0 and r.stderr:
        print(r.stderr[-2000:], file=sys.stderr)
    return r.returncode, r.stdout


def run_task(task: Task, *, task_dir: Path, harbor: str = "harbor",
             agent: str = "claude-code", model: str = "anthropic/claude-sonnet-4-5",
             extra_env: dict | None = None) -> Path | None:
    """Run `harbor run` on the generated task dir; collect git diff as patch."""
    env = dict(os.environ)
    anth_env = _anthropic_env()
    env.update(anth_env)
    if extra_env:
        env.update(extra_env)
    # Harbor doesn't auto-forward host env into the container, so write a
    # .env and pass --env-file (keeps the token out of process args).
    env_file = task_dir / ".env"
    env_lines = [f"{k}={v}" for k, v in anth_env.items()]
    if extra_env:
        env_lines += [f"{k}={v}" for k, v in extra_env.items()]
    env_file.write_text("\n".join(env_lines) + "\n")
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    code, _ = _run([
        harbor, "run",
        "-p", str(task_dir),
        "-a", agent,
        "-m", model,
        "--env-file", str(env_file),
        "--timeout-multiplier", "2",
    ], env=env)
    # Harbor writes trial artifacts under task_dir/trials/<trial>/...
    # The model_patch: extract git diff from the agent's working copy.
    patch = _extract_patch(task_dir, instance_id=task.instance_id)
    if patch is None:
        print(f"  ! no patch collected for {task.instance_id}", file=sys.stderr)
        return None
    PREDICTIONS_DIR.mkdir(exist_ok=True)
    pred_path = PREDICTIONS_DIR / f"{task.instance_id}_preds.json"
    pred = {
        task.instance_id: {
            "model_name_or_path": model,
            "instance_id": task.instance_id,
            "model_patch": patch,
        }
    }
    pred_path.write_text(json.dumps(pred, ensure_ascii=False, indent=2))
    print(f"  ✓ wrote {pred_path}")
    return pred_path


def _strip_test_hunks(patch: str) -> str:
    """Remove diff hunks for test files (tests/...). The gold test_patch
    supplies tests; overlapping hunks make `git apply` fail in eval."""
    import re
    parts = re.split(r"(?=^diff --git )", patch, flags=re.M)
    return "".join(
        p for p in parts
        if p.strip() and not re.match(r"diff --git a/tests?/", p)
    )


def _extract_patch(task_dir: Path, instance_id: str | None = None) -> str | None:
    """Find the model.patch that the task's test.sh wrote into Harbor's trial
    artifacts (it captured the agent's `git diff` inside the container).

    Harbor's jobs_dir defaults to `jobs/` relative to the harbor CWD, which is
    the bench/ dir when run via `bench run`. So we search several roots.
    """
    roots: list[Path] = [
        task_dir,                                   # runs/...
        task_dir.parent.parent,                     # bench/ (runs/<setting>/<id>)
        Path.cwd(),                                 # wherever bench was launched
        Path(os.environ.get("SWECB_ROOT", str(task_dir.parent.parent))),
    ]
    # de-dup
    seen = set()
    roots = [r for r in roots if r not in seen and not seen.add(r)]  # type: ignore[func-returns-value]
    candidates: list[Path] = []
    for r in roots:
        if not r.exists():
            continue
        # prefer this instance's trial dir, then any
        if instance_id:
            candidates.extend(r.glob(f"jobs/**/{instance_id}__*/verifier/model.patch"))
            candidates.extend(r.glob(f"jobs/**/{instance_id}__*/verifier/model.patch"))
        candidates.extend(r.glob("jobs/**/verifier/model.patch"))
        candidates.extend(r.glob("**/verifier/model.patch"))
    # newest first
    candidates = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            txt = p.read_text()
        except Exception:
            continue
        if txt.strip():
            return _strip_test_hunks(txt)
    return None


def eval_run(run_id: str, subset: str = "lite") -> int:
    """Hand off to the SWEContextBench evaluation harness."""
    script = Path(os.environ.get("SWECB_ROOT", os.getcwd())) / "evaluation.sh"
    return subprocess.call(["bash", str(script), run_id, subset])
