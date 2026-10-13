"""Decide from file contents whether a changed pyproject.toml or uv.lock can change what the tests do."""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Collection
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mutmut.coverage_map import ProjectPaths
from mutmut.workspace import parse_toml

# The default patterns that stand for these two files; any other pattern matching them stays blanket.
ROUTED_PATTERNS = frozenset({"*pyproject.toml", "*.lock"})
LOCK_VERSION = 1
DEPENDENCY_TABLES = (("project", "dependencies"), ("project", "optional-dependencies"), ("dependency-groups",))
# A (name, extra) pair to visit; extra "" is the package's own dependencies.
Node = tuple[str, str]


class Undecidable(ValueError):
    """The change can't be judged from the files, so every test has to run."""


def content_aware_reason(
    paths: ProjectPaths, commit: str, changed: Collection[str], patterns: Collection[str]
) -> tuple[str | None, set[str]]:
    """Why a pyproject.toml or uv.lock change since ``commit`` reruns everything, and the changes it settled."""
    reason, settled = None, set()
    for path in sorted(changed):
        name = Path(path).name
        hits = {pattern for pattern in patterns if fnmatch.fnmatch(path, pattern)}
        if name not in ("pyproject.toml", "uv.lock") or not hits or not hits <= ROUTED_PATTERNS:
            continue
        settled.add(path)
        if reason is None and (fragment := judge(paths, commit, path)):
            reason = f"{path} {fragment}"
    return reason, settled


def judge(paths: ProjectPaths, commit: str, path: str) -> str | None:
    """What about this file's change reruns every test, or None if nothing does."""
    old, new = paths.file_at(commit, path), read_text(paths.repo / path)
    try:
        if old is None or new is None:
            raise Undecidable("old or new contents unavailable")
        if path.endswith("uv.lock"):
            return lock_change(old, new, paths.project_name(), is_governing_lock(paths, path))
        return pyproject_change(old, new, has_lock=nearest_lock(paths, path) is not None)
    except ValueError:  # unparseable or unknown: assume the worst
        return "changed (could not compare it with the old one)"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text("utf-8")
    except OSError:
        return None


def nearest_lock(paths: ProjectPaths, path: str) -> Path | None:
    for directory in (paths.repo / path).parents:
        if (directory / "uv.lock").is_file():
            return directory / "uv.lock"
        if directory == paths.repo:
            break
    return None


def is_governing_lock(paths: ProjectPaths, path: str) -> bool:
    """Whether the lock sits in the project's directory or above it, so it locks the project's environment."""
    return paths.project.is_relative_to((paths.repo / path).parent)


def pyproject_change(old: str, new: str, has_lock: bool) -> str | None:
    """The first table of a changed pyproject.toml that can change the tests, as ``[tool.x] changed``."""
    before, after = parse_toml(old), parse_toml(new)
    tables: list[tuple[tuple[str, ...], int]] = [(("tool", "pytest"), 1), (("tool", "coverage"), 1)]
    tables.append((("tool", "mutmut"), 0))
    for keys, depth in tables:
        if table := first_difference(select_table(before, keys), select_table(after, keys), keys, depth):
            return f"[{table}] changed"
    if not has_lock:
        for keys in DEPENDENCY_TABLES:
            if select_table(before, keys) != select_table(after, keys):
                return f"[{'.'.join(keys)}] changed"
    return None


def select_table(document: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """A table by its path, without mutmut's list of tests, which only picks tests."""
    table: Any = document
    for key in keys:
        table = table.get(key, {}) if isinstance(table, dict) else {}
    if keys == ("tool", "mutmut") and isinstance(table, dict):
        return {k: v for k, v in table.items() if k != "pytest_add_cli_args_test_selection"}
    return table


def first_difference(old: Any, new: Any, keys: tuple[str, ...], depth: int) -> str | None:
    """The dotted name of the deepest table, down ``depth`` levels, in which old and new differ."""
    if old == new:
        return None
    if depth and isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            child_old, child_new = old.get(key), new.get(key)
            if isinstance(child_old, dict) or isinstance(child_new, dict):
                if found := first_difference(child_old or {}, child_new or {}, (*keys, key), depth - 1):
                    return found
    return ".".join(keys)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(text: str) -> dict[str, Any]:
    lock = parse_toml(text)
    if lock.get("version") != LOCK_VERSION:
        raise Undecidable(f"unknown uv.lock version {lock.get('version')!r}")
    return lock


def packages_by_name(lock: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for package in lock.get("package", []):
        found.setdefault(normalize(package["name"]), []).append(package)
    return found


def fingerprint(entries: Iterable[dict[str, Any]]) -> frozenset[tuple[str, str]]:
    """What makes a distribution a different one: each entry's version and source."""
    return frozenset((entry.get("version", ""), json.dumps(entry.get("source"), sort_keys=True)) for entry in entries)


def requirements(items: Iterable[dict[str, Any]]) -> list[Node]:
    """The packages, and extras of them, a list of lock requirements asks for."""
    nodes: list[Node] = []
    for item in items:
        name = normalize(item["name"])
        nodes.append((name, ""))
        nodes.extend((name, extra) for extra in item.get("extra", item.get("extras", [])))
    return nodes


def used_distributions(lock: dict[str, Any], project: str) -> set[str]:
    """The distributions in the project's environment: its own requirements, extras and groups, and the root's groups."""
    by_name = packages_by_name(lock)
    manifest = lock.get("manifest", {})
    pending: list[Node] = [(project, "")]
    pending += requirements(manifest.get("requirements", []))
    for group in manifest.get("dependency-groups", {}).values():
        pending += requirements(group)
    for entry in by_name.get(project, []):
        pending += [(project, extra) for extra in entry.get("optional-dependencies", {})]
        for group in entry.get("dev-dependencies", {}).values():
            pending += requirements(group)
    seen: set[Node] = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        name, extra = node
        if extra:
            pending.append((name, ""))
        for entry in by_name.get(name, []):
            items = entry.get("optional-dependencies", {}).get(extra, []) if extra else entry.get("dependencies", [])
            pending += requirements(items)
    return {name for name, _ in seen}


def lock_change(old: str, new: str, project: str | None, governing: bool) -> str | None:
    """The changed distributions the project's environment holds, as ``changed a, b used by project``."""
    if project is None or not governing:
        raise Undecidable("the project is not named by a uv.lock above it")
    before, after = read_lock(old), read_lock(new)
    if project not in packages_by_name(after):
        raise Undecidable("the project is not in the lock")
    before_by_name, after_by_name = packages_by_name(before), packages_by_name(after)
    old_used = used_distributions(before, project) if project in before_by_name else set()
    new_used = used_distributions(after, project)
    changed = {
        name
        for name in before_by_name.keys() | after_by_name.keys()
        if fingerprint(before_by_name.get(name, [])) != fingerprint(after_by_name.get(name, []))
    }
    hits = sorted(changed & (old_used | new_used) | (old_used ^ new_used))
    if not hits:
        return None
    shown = ", ".join(hits[:3]) + (f" and {len(hits) - 3} more" if len(hits) > 3 else "")
    return f"changed {shown} used by {project}"
