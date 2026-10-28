"""Load SWE-ContextBench task instances."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass

SUBSETS = {
    "lite": "SWEContextBench Lite",
    "multilingual": "SWEContextBench Multilingual",
    "verified": "SWEContextBench Verified",
    "full": "SWEContextBench Full",
}

REPO_ROOT = os.environ.get(
    "SWECB_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)


@dataclass
class Task:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    test_patch: str
    version: str
    environment_setup_commit: str

    @property
    def user_id(self) -> str:
        """repo-based retrieval scope (e.g. 'django')."""
        return self.instance_id.split("__", 1)[0] or "unknown"


def _path(subset: str) -> str:
    return os.path.join(REPO_ROOT, "cases", SUBSETS[subset])


def load_tasks(subset: str = "lite",
               only: list[str] | None = None) -> list[Task]:
    d = _path(subset)
    tasks: list[Task] = []
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        with open(f) as fh:
            o = json.load(fh)
        iid = o["instance_id"]
        if only and iid not in only:
            continue
        tasks.append(Task(
            instance_id=iid,
            repo=o["repo"],
            base_commit=o["base_commit"],
            problem_statement=o["problem_statement"],
            fail_to_pass=o.get("FAIL_TO_PASS", []),
            pass_to_pass=o.get("PASS_TO_PASS", []),
            test_patch=o.get("test_patch", ""),
            version=o.get("version", ""),
            environment_setup_commit=o.get("environment_setup_commit", o["base_commit"]),
        ))
    return tasks


def past_experience_glob(subset: str = "lite") -> str:
    d = os.path.join(REPO_ROOT, "cases", f"SWEContextBench {subset.title()} Past Experience")
    return os.path.join(d, "*.jsonl")
