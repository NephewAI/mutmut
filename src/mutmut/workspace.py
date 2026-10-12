"""Run one diff-scoped mutmut per changed member of a uv workspace, and judge them together."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_toml(path: Path) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        from tomllib import loads
    else:
        # noinspection PyPackageRequirements
        from toml import loads
    return loads(path.read_text("utf-8"))


@dataclass(frozen=True)
class MemberRun:
    member: Path
    exit_code: int
    output: str


def workspace_members(root: Path) -> list[Path]:
    """The member directories ``[tool.uv.workspace]`` names, relative to ``root``."""
    workspace = load_toml(root / "pyproject.toml").get("tool", {}).get("uv", {}).get("workspace", {})
    excluded = {path for pattern in workspace.get("exclude", []) for path in root.glob(pattern)}
    members = {path for pattern in workspace.get("members", []) for path in root.glob(pattern)} - excluded
    return sorted(path.relative_to(root) for path in members if (path / "pyproject.toml").is_file())


def changed_members(root: Path, base: str) -> list[Path]:
    """Members with a changed Python file since ``base``; the others have nothing to mutate."""
    command = ["git", "diff", "--name-only", "--relative", base, "--"]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"git diff against {base} failed: {result.stderr.strip()}")
    changed = [Path(line) for line in result.stdout.splitlines() if line.endswith(".py")]
    return [member for member in workspace_members(root) if any(path.is_relative_to(member) for path in changed)]


def run_members(root: Path, members: Sequence[Path], arguments: Sequence[str], cpu_count: int) -> list[MemberRun]:
    """``mutmut run`` in each member, side by side, each with its own temp dir and share of the CPUs."""
    if not members:
        return []
    parallelism = min(cpu_count, len(members))
    children = max(1, cpu_count // parallelism)

    def run(member: Path) -> MemberRun:
        with tempfile.TemporaryDirectory(prefix="mutmut-") as scratch:
            # Concurrent pytest runs prune each other's numbered temp dirs unless they have separate roots.
            environment = {**os.environ, "TMPDIR": scratch}
            command = [sys.executable, "-m", "mutmut", "run", "--max-children", str(children), *arguments]
            result = subprocess.run(
                command, cwd=root / member, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        return MemberRun(member, result.returncode, result.stdout)

    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        return list(executor.map(run, members))


def report(runs: Sequence[MemberRun]) -> int:
    """Print each member's output as one block, then the combined verdict; returns the exit code."""
    for run in runs:
        print(f"\n===== {run.member} (exit {run.exit_code}) =====\n{run.output}")
    failed = [str(run.member) for run in runs if run.exit_code != 0]
    if failed:
        print(f"\nMutation testing failed for: {', '.join(failed)}")
        return 1
    print(f"\nMutation testing passed for {len(runs)} changed member(s).")
    return 0
