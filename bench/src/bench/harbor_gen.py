"""Generate Harbor task directory + JobConfig per setting.

Settings (all stack baseline_hygiene):
- no-context    : baseline only, NO plugin
- free-context  : baseline + MCP(backend=raw trajectories) + skill + hooks
- free-summary  : same as free-context but backend pool = LLM summaries
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# harbor_gen.py lives at <root>/bench/src/bench/harbor_gen.py → parents[3] is root.
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "memory-plugin"
BRIDGE_PKG = "memory-bridge"  # uvx-installed

# Where the bridge store lives inside the container
STORE_PATH_IN_CONTAINER = "/memory/memory_store.jsonl"
SKILLS_DIR_IN_CONTAINER = "/plugin/skills"
HOOKS_DIR_IN_CONTAINER = "/plugin/hooks"


def _task_toml(setting: str, *, backend: str = "trivial",
               docker_image: str | None = None,
               agent_timeout: float = 3600.0,
               verify_timeout: float = 1800.0,
               host_store_path: str | None = None) -> str:
    """Emit task.toml. For Free settings we bind-mount the plugin dir and the
    memory store from the host into the container, otherwise the in-container
    MCP/skill/hooks paths would be empty (the prebuilt instance image has none
    of our plugin files) and the agent would search an empty store."""
    lines = [
        'version = "1.0"',
        "",
        "[verifier]",
        f"timeout_sec = {verify_timeout}",
        "",
        "[agent]",
        f"timeout_sec = {agent_timeout}",
        "",
        "[environment]",
    ]
    if docker_image:
        lines.append(f'docker_image = "{docker_image}"')
    if setting != "no-context":
        # mounts: plugin (skills+hooks) + memory store, both read-only — the
        # agent only searches, never writes (paper's fixed-pool setting).
        lines.append(f'skills_dir = "{SKILLS_DIR_IN_CONTAINER}"')
        lines.append("")
        lines.append("[[environment.mounts]]")
        lines.append('type = "bind"')
        lines.append(f'source = "{PLUGIN_DIR}"')
        lines.append(f'target = "/plugin"')
        lines.append('read_only = true')
        if host_store_path:
            lines.append("")
            lines.append("[[environment.mounts]]")
            lines.append('type = "bind"')
            lines.append(f'source = "{host_store_path}"')
            lines.append(f'target = "{STORE_PATH_IN_CONTAINER}"')
            lines.append('read_only = true')
        lines.append("")
        lines.append("[[environment.mcp_servers]]")
        lines.append('name = "memory"')
        lines.append('transport = "stdio"')
        lines.append('command = "uvx"')
        lines.append(
            f'args = ["{BRIDGE_PKG}", "serve-mcp", "--backend", '
            f'"{backend}", "--store-path", "{STORE_PATH_IN_CONTAINER}"]'
        )
    return "\n".join(lines) + "\n"


def _jobconfig(setting: str, *, agent: str, model: str,
               backend: str, backend_url: str | None) -> dict:
    cfg: dict = {
        "agent": {"name": agent, "model_name": model},
        "env": {},
    }
    # baseline hygiene permissions (all settings)
    cfg["agent"]["kwargs"] = {
        "config": {
            "permissions": {
                "deny": [
                    "AskUserQuestion",
                    "Write(**/MEMORY.md)", "Edit(**/MEMORY.md)",
                    "Write(**/CLAUDE.md)", "Edit(**/CLAUDE.md)",
                    "Write(**/summary.md)", "Edit(**/summary.md)",
                    "Write(**/.claude/projects/*/memory/**)",
                    "Edit(**/.claude/projects/*/memory/**)",
                ]
            },
            "enableAllProjectMcpServers": False,
        }
    }
    if setting != "no-context":
        # hook env so hook scripts know which backend to talk to
        cfg["env"].update({
            "MB_BACKEND": backend,
            "MB_STORE_PATH": STORE_PATH_IN_CONTAINER,
            "MB_BACKEND_URL": backend_url or "",
        })
        cfg["agent"]["kwargs"]["config"]["hooks"] = {
            "PreToolUse": [{
                "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command",
                           "command": f"{HOOKS_DIR_IN_CONTAINER}/pretool_block_native_memory.sh"}],
            }],
        }
        # SessionStart auto-inject (read-only retrieval). The write-back hooks
        # (PostToolUse capture, Stop save) are deliberately OMITTED: the paper's
        # "Free Context" setting uses a FIXED memory pool, and writing each
        # session's result back would pollute the pool across the 99 tasks
        # (task N's solution leaking into task N+1's retrieval). The agent is
        # also instructed "Do not save the new issue to memory".
        cfg["agent"]["kwargs"]["config"]["hooks"].setdefault(
            "SessionStart", []).append({
                "matcher": "*",
                "hooks": [{"type": "command",
                           "command": f"{HOOKS_DIR_IN_CONTAINER}/session_start.sh"}],
            })
    return cfg


def instance_image_name(instance_id: str) -> str:
    """Pre-built instance image on Docker Hub (authors provide these).
    Same tag convention as run_evaluation.py: jiayuanz3/swecontextbench:<id>.
    No need to build_instance / buildx — just `docker pull`."""
    return f"jiayuanz3/swecontextbench:{instance_id.replace('__', '.').lower()}"


_PATCH_CAPTURE_SH = """#!/bin/bash
# Harbor verifier: capture the agent's git diff as model_patch, always reward=1.
# Real judging is done later by SWEContextBench evaluation.sh (FAIL_TO_PASS).
# IMPORTANT: exclude test files from the captured diff — the gold test_patch
# supplies the tests, and overlapping hunks would make `git apply` fail.
set -uo pipefail
mkdir -p /logs/verifier
# find the repo root (SWE-bench images keep it under /testbed or the workdir)
REPO=""
for d in /testbed /app /workspace "$PWD"; do
  if [ -d "$d/.git" ] || git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then REPO="$d"; break; fi
done
if [ -z "$REPO" ]; then
  REPO=$(find / -maxdepth 4 -name .git -type d 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
fi
if [ -n "$REPO" ]; then
  git -C "$REPO" diff -- . ':!tests/' ':!test/' > /logs/verifier/model.patch 2>/dev/null || true
  git -C "$REPO" status --short > /logs/verifier/git_status.txt 2>/dev/null || true
fi
echo 1 > /logs/verifier/reward.txt
"""


def write_task_dir(task_dir: Path, *, task, setting: str, agent: str,
                   model: str, backend: str, backend_url: str | None,
                   use_instance_image: bool = True,
                   host_store_path: str | None = None) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    # instruction.md = problem statement (+ prompt to use memory in Free)
    instr = task.problem_statement
    if setting != "no-context":
        instr = (
            "When asked to fix a bug, always call `search_memories` first with "
            "this problem statement "
            f"and user_id={task.user_id} to search for past similar bugs and "
            "relevant context before attempting a fix. If relevant past context "
            "is found, learn from it to solve the new issue. "
            "Do not save the new issue to memory.\n\n---\n\n"
            + instr
        )
    (task_dir / "instruction.md").write_text(instr)
    img = instance_image_name(task.instance_id) if use_instance_image else None
    (task_dir / "task.toml").write_text(_task_toml(setting, backend=backend,
                                                   docker_image=img,
                                                   host_store_path=host_store_path))
    # tests/test.sh: capture the agent's diff as model.patch
    tests = task_dir / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test.sh").write_text(_PATCH_CAPTURE_SH)
    os.chmod(tests / "test.sh", 0o755)
    # environment/ dir must exist for harbor task resolution even when we use a
    # prebuilt `docker_image` (no Dockerfile). Create it empty in that case.
    (task_dir / "environment").mkdir(exist_ok=True)
    # No environment/Dockerfile — we use the prebuilt instance image via
    # task.toml [environment] docker_image.
    # JobConfig yaml (write as .yaml for harbor --config)
    jc = _jobconfig(setting, agent=agent, model=model,
                   backend=backend, backend_url=backend_url)
    import yaml
    (task_dir / "job.yaml").write_text(yaml.safe_dump(jc, sort_keys=False))
