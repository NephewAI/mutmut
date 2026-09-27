"""Scope a run to the lines a diff changed, and judge it like a CI gate."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.file_mutation import mutate_file_contents
from mutmut.stats import status_by_exit_code

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
RESOLVED_STATUSES = frozenset({"killed", "caught by type check"})


@dataclass(frozen=True)
class DiffScope:
    """New-side lines changed since a git ref, per source file, relative to the project."""

    base: str
    changed_lines: Mapping[str, frozenset[int]]

    @classmethod
    def from_git(cls, base: str, source_files: Iterable[Path]) -> DiffScope:
        """Committed, staged and unstaged changes against ``base``, in the given source files."""
        command = ["git", "diff", "-U0", "-M", "--relative", "--no-prefix", "--no-color", base, "--"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f"git diff against {base} failed: {result.stderr.strip()}")
        changed = parse_changed_lines(result.stdout)
        return cls(base, {str(path): changed[str(path)] for path in source_files if changed.get(str(path))})

    def has_possible_mutants(self) -> bool:
        """Whether any changed line has a mutant, as if all were covered; if not, no run can find one."""
        return any(
            mutate_file_contents(path, Path(path).read_text(), set(lines)).mutant_names
            for path, lines in self.changed_lines.items()
        )

    def shard(self, index: int, count: int) -> DiffScope:
        """This shard's share of the changed functions; each is placed by its own hash, so other changes can't move it."""
        kept: dict[str, set[int]] = {}
        for path, lines in self.changed_lines.items():
            for unit in split_by_function(Path(path).read_text(), lines):
                digest = hashlib.sha256(f"{path}:{sorted(unit)}".encode()).digest()
                if int.from_bytes(digest[:8], "big") % count == index:
                    kept.setdefault(path, set()).update(unit)
        return DiffScope(self.base, {path: frozenset(lines) for path, lines in kept.items()})

    def forget_previous_mutants(self) -> None:
        """Drop earlier mutants and verdicts of the changed files: they were scoped to other lines,
        and cached verdicts outlive changes to the tests that produced them."""
        for path in self.changed_lines:
            for stale in (Path("mutants") / path, Path("mutants") / f"{path}.meta"):
                stale.unlink(missing_ok=True)

    def line_filter(self, covered: Mapping[str, set[int]] | None) -> dict[str, set[int]]:
        """The lines to mutate, keyed like the coverage data: changed, and covered if that is measured."""
        lines = {}
        for path, changed in self.changed_lines.items():
            key = str((Path("mutants") / path).absolute())
            lines[key] = set(changed) if covered is None else set(changed) & covered.get(key, set())
        return lines


def split_by_function(source: str, lines: Iterable[int]) -> list[frozenset[int]]:
    """Group lines by their innermost function; module-level lines stay together."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [frozenset(lines)]
    spans = sorted(
        (
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        key=lambda span: span[1] - span[0],
    )
    grouped: dict[tuple[int, int], set[int]] = {}
    for line in sorted(lines):
        span = next((span for span in spans if span[0] <= line <= span[1]), (0, 0))
        grouped.setdefault(span, set()).add(line)
    return [frozenset(grouped[span]) for span in sorted(grouped)]


def parse_changed_lines(diff: str) -> dict[str, frozenset[int]]:
    """New-side line numbers of each file in a ``git diff -U0 --no-prefix``."""
    changed: dict[str, set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target
            if path is not None:
                changed.setdefault(path, set())
        elif path is not None and (match := HUNK_HEADER.match(line)):
            start, count = int(match[1]), 1 if match[2] is None else int(match[2])
            changed[path].update(range(start, start + count))
    return {path: frozenset(lines) for path, lines in changed.items()}


@dataclass(frozen=True)
class FileVerdict:
    path: str
    changed: int
    covered: int | None
    exit_codes: Mapping[str, int | None]

    @property
    def unresolved(self) -> list[str]:
        return [
            f"{name} ({status_by_exit_code[code]})"
            for name, code in sorted(self.exit_codes.items())
            if status_by_exit_code[code] not in RESOLVED_STATUSES
        ]

    def describe(self) -> str:
        covered = "" if self.covered is None else f" covered={self.covered}"
        resolved = len(self.exit_codes) - len(self.unresolved)
        return f"{self.path}: changed={self.changed}{covered} mutants={len(self.exit_codes)} resolved={resolved}"


def judge(scope: DiffScope, covered_lines: Mapping[str, set[int]] | None) -> list[FileVerdict]:
    """Each changed file's mutants and whether every one was killed or caught by the type checker."""
    verdicts = []
    for path, changed in sorted(scope.changed_lines.items()):
        data = SourceFileMutationData(path=path)
        data.load()
        key = str((Path("mutants") / path).absolute())
        covered = None if covered_lines is None else len(changed & covered_lines.get(key, set()))
        verdicts.append(FileVerdict(path, len(changed), covered, dict(data.exit_code_by_key)))
    return verdicts
