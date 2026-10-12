"""Remember which files each test runs, so a diff run can skip the tests its change can't reach."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from mutmut.utils.format_utils import strip_prefix
from mutmut.workspace import parse_toml

if TYPE_CHECKING:
    import pytest
    from coverage import CoverageData

MAP_PATH = Path("mutants/mutmut-coverage-map.json")
COLLECT_CONTEXT = "collect|"


def git(repo: Path | None, *args: str) -> str | None:
    command = ["git", *(["-C", str(repo)] if repo else []), *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class ProjectPaths:
    """The git repository and the project inside it, for turning measured files into repository paths."""

    repo: Path
    project: Path
    # Directories outside the repository mapped to the repository directory they mount, or None.
    mounts: dict[Path, Path | None] = field(default_factory=dict, compare=False)

    @classmethod
    def here(cls) -> ProjectPaths | None:
        top = git(None, "rev-parse", "--show-toplevel")
        return None if top is None else cls(Path(top.strip()).resolve(), Path.cwd().resolve())

    def in_repo(self, measured: str) -> str | None:
        """A measured file relative to the repository, with the `mutants/` copy read as its original."""
        path = Path(measured).resolve()
        inside = path if path.is_relative_to(self.repo) else self.through_mount(path)
        if inside is None:
            return None
        if inside.is_relative_to(mutants := self.project / "mutants"):
            inside = self.project / inside.relative_to(mutants)
        return str(inside.relative_to(self.repo))

    def through_mount(self, path: Path) -> Path | None:
        """The repository file an outside file is, as when a container imports the checkout from another path."""
        for outside, inside in self.mounts.items():
            if path.is_relative_to(outside):
                return inside / path.relative_to(outside) if inside else None
        anchors = [self.project, *(parent for parent in self.project.parents if parent.is_relative_to(self.repo))]
        for i in range(1, len(path.parts) - 1):
            tail = Path(*path.parts[i:])
            for anchor in anchors:
                if (anchor / tail).is_file() and os.path.samefile(anchor / tail, path):
                    self.mounts[Path(*path.parts[:i])] = anchor
                    return anchor / tail
        self.mounts[path.parent] = None
        return None

    def in_project(self, repo_path: str) -> str | None:
        path = self.repo / repo_path
        return str(path.relative_to(self.project)) if path.is_relative_to(self.project) else None

    def head(self) -> str | None:
        """The commit the working tree matches, or None if tracked files have changed since it."""
        if git(self.repo, "diff", "--quiet", "HEAD", "--") is None:
            return None
        head = git(self.repo, "rev-parse", "HEAD")
        return head.strip() if head else None

    def changed_since(self, commit: str) -> set[str] | None:
        """Repository paths changed since ``commit``, committed or not; None if git can't tell.

        A pyproject.toml counts only if its test settings changed.
        """
        diff = git(self.repo, "diff", "--name-only", "--no-renames", commit, "--")
        untracked = git(self.repo, "ls-files", "--others", "--exclude-standard")
        if diff is None or untracked is None:
            return None
        mutants = str((self.project / "mutants").relative_to(self.repo)) + "/"
        return {
            line
            for line in (diff + untracked).splitlines()
            if line
            and not line.startswith(mutants)
            and (Path(line).name != "pyproject.toml" or self.test_settings_changed(commit, line))
        }

    def test_settings_changed(self, commit: str, path: str) -> bool:
        before, after = git(self.repo, "show", f"{commit}:{path}"), self.repo / path
        if before is None or not after.is_file():
            return True
        try:
            return test_settings(parse_toml(before)) != test_settings(parse_toml(after.read_text()))
        except ValueError:  # unparseable: assume the worst
            return True


# Tool tables that only configure linters and type checkers, which never run inside a test.
STATIC_TOOLS = frozenset(
    {"ruff", "mypy", "pyrefly", "pyright", "vulture", "tach", "importlinter", "black", "isort", "pylint", "codespell"}
)


def test_settings(pyproject: dict[str, Any]) -> dict[str, Any]:
    """A pyproject.toml without what can't change a test: static tool settings, and mutmut's list of tests."""
    tool = {name: table for name, table in pyproject.get("tool", {}).items() if name not in STATIC_TOOLS}
    if "mutmut" in tool:
        tool["mutmut"] = {k: v for k, v in tool["mutmut"].items() if k != "pytest_add_cli_args_test_selection"}
    return {**pyproject, "tool": tool}


def opened_files(opened: Mapping[str, Iterable[str]], paths: ProjectPaths) -> dict[str, frozenset[str]]:
    """The repository files each context opened, such as data files and templates, leaving out bytecode."""
    in_repo = cache(paths.in_repo)
    return {
        context: frozenset(path for name in names if "__pycache__" not in name and (path := in_repo(name)) is not None)
        for context, names in opened.items()
    }


def files_by_context(data: CoverageData, paths: ProjectPaths) -> dict[str, frozenset[str]]:
    """The repository files each coverage context ran a line of."""
    in_repo = cache(paths.in_repo)
    found: dict[str, set[str]] = defaultdict(set)
    for measured, context in file_context_pairs(data):
        if (path := in_repo(measured)) is not None:
            found[context].add(path)
    return {context: frozenset(files) for context, files in found.items()}


def file_context_pairs(data: CoverageData) -> list[tuple[str, str]]:
    """Each file and context with a recorded line, in one query: reading every file's lines is ten times slower."""
    table = "arc" if data.has_arcs() else "line_bits"
    query = f"""
        SELECT DISTINCT file.path, context.context FROM {table}
        JOIN file ON file.id = {table}.file_id JOIN context ON context.id = {table}.context_id
    """
    data.measured_files()  # loads the data before the private connection reads it
    with data._connect() as connection, connection.execute(query) as rows:
        return list(rows)


def module_of(test: str) -> str:
    return test.split("::", 1)[0]


@dataclass(frozen=True)
class Selection:
    """The tests a change can reach; pytest skips the rest, and doesn't import modules with none of them."""

    tests: frozenset[str]
    modules: frozenset[str]
    known_tests: frozenset[str]
    known_modules: frozenset[str]

    def pytest_ignore_collect(self, collection_path: Path, config: pytest.Config) -> bool | None:
        if not collection_path.is_relative_to(config.rootpath):
            return None
        module = str(collection_path.relative_to(config.rootpath))
        if module in self.known_modules and module not in self.modules:
            return True
        return None

    def pytest_collection_modifyitems(self, items: list[pytest.Item], config: pytest.Config) -> None:
        kept: list[pytest.Item] = []
        dropped: list[pytest.Item] = []
        for item in items:
            test = strip_prefix(item.nodeid, prefix="mutants/")
            (kept if test in self.tests or test not in self.known_tests else dropped).append(item)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
            items[:] = kept


@dataclass(frozen=True)
class CoverageMap:
    """The files each test ran in its setup, call and teardown, and that each test module's import ran, at a commit."""

    commit: str
    files_by_test: Mapping[str, frozenset[str]]
    files_by_module: Mapping[str, frozenset[str]]

    @classmethod
    def from_contexts(cls, commit: str, contexts: Mapping[str, frozenset[str]]) -> CoverageMap:
        tests = {
            context: files for context, files in contexts.items() if context and not context.startswith(COLLECT_CONTEXT)
        }
        modules = {
            strip_prefix(c, prefix=COLLECT_CONTEXT): f for c, f in contexts.items() if c.startswith(COLLECT_CONTEXT)
        }
        return cls(commit, tests, modules)

    @classmethod
    def load(cls, path: Path = MAP_PATH) -> CoverageMap | None:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        files = data["files"]

        def expand(indexed: dict[str, list[int]]) -> dict[str, frozenset[str]]:
            return {key: frozenset(files[i] for i in indexes) for key, indexes in indexed.items()}

        return cls(data["commit"], expand(data["tests"]), expand(data["modules"]))

    def save(self, path: Path = MAP_PATH) -> None:
        index: dict[str, int] = {}

        def compress(by_key: Mapping[str, frozenset[str]]) -> dict[str, list[int]]:
            return {key: sorted(index.setdefault(file, len(index)) for file in files) for key, files in by_key.items()}

        tests, modules = compress(self.files_by_test), compress(self.files_by_module)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"commit": self.commit, "files": list(index), "tests": tests, "modules": modules}))

    def merged(self, newer: CoverageMap) -> CoverageMap:
        """Both maps' files for every test, at the newer commit: a test can run different files from run to run."""

        def union(old: Mapping[str, frozenset[str]], new: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
            return {key: old.get(key, frozenset()) | new.get(key, frozenset()) for key in old.keys() | new.keys()}

        return CoverageMap(
            newer.commit,
            union(self.files_by_test, newer.files_by_test),
            union(self.files_by_module, newer.files_by_module),
        )

    def select(self, changed: Iterable[str], rerun_all_patterns: Iterable[str]) -> Selection | str:
        """The tests that ran a changed file at this map's commit, or why every test has to run."""
        changed = set(changed)
        for pattern in rerun_all_patterns:
            if hits := fnmatch.filter(changed, pattern):
                return f"{hits[0]} changed"
        tests = frozenset(
            test
            for test, files in self.files_by_test.items()
            if not files.isdisjoint(changed)
            or not self.files_by_module.get(module_of(test), frozenset()).isdisjoint(changed)
        )
        known_modules = frozenset(self.files_by_module) | {module_of(test) for test in self.files_by_test}
        modules = frozenset(module_of(test) for test in tests)
        return Selection(tests, modules, frozenset(self.files_by_test), known_modules)
