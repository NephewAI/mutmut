from __future__ import annotations

import ast
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

import coverage
from coverage import CoverageData
from coverage.exceptions import CoverageException

from mutmut.configuration import config
from mutmut.utils.format_utils import get_mutant_name
from mutmut.utils.format_utils import make_mutant_key
from mutmut.utils.format_utils import strip_prefix

if TYPE_CHECKING:
    from _pytest.nodes import Item
    from _pytest.runner import CallInfo

    from mutmut.runners.harness import TestRunner


@dataclass
class CoverageInfo:
    """What coverage.py knows about the source files, keyed by absolute path in `mutants/`.

    `covered_lines` are the lines that actually executed. `excluded_lines` are the lines
    coverage.py was told to ignore (`# pragma: no cover`, `exclude_lines`, `exclude_also`).
    The two are kept apart rather than subtracted, because an excluded line that runs is
    also a covered line, and the two are used differently: coverage.py reports only the
    *first* line of each excluded statement, so those have to be expanded to full
    statements before they mean anything to the mutation visitor.
    """

    covered_lines: dict[str, set[int]] = field(default_factory=dict)
    excluded_lines: dict[str, set[int]] = field(default_factory=dict)
    # Filled only with `stats_from_coverage`: what a separate stats run would record.
    tests_by_mangled_function_name: dict[str, set[str]] = field(default_factory=dict)
    duration_by_test: dict[str, float] = field(default_factory=dict)
    test_exit_code: int = 0


# Returns a set of lines that are covered in this file gvein the covered_lines dict
#  returned by gather_coverage
# None means it's not enabled, set() means no lines are covered
def get_covered_lines_for_file(filename: str, covered_lines: dict[str, set[int]] | None) -> set[int] | None:
    if covered_lines is None or filename is None:
        return None

    abs_filename = str((Path("mutants") / filename).absolute())
    lines: set[int] = set()
    if abs_filename in covered_lines:
        lines = set(covered_lines[abs_filename])

    return lines


# Returns the lines coverage.py excludes from measurement in this file, given the
# excluded_lines dict returned by gather_coverage. An empty set means nothing is excluded,
# which is also what we get when the feature is disabled.
def get_excluded_lines_for_file(filename: str, excluded_lines: dict[str, set[int]] | None) -> set[int]:
    if excluded_lines is None or filename is None:
        return set()

    abs_filename = str((Path("mutants") / filename).absolute())
    return set(excluded_lines.get(abs_filename, ()))


def gather_coverage(runner: TestRunner, source_files: Iterable[Path]) -> CoverageInfo:
    """Gathers coverage for the given source files in a throwaway child process.

    Returns the covered and excluded lines of each of them. Forking keeps the test
    suite's imports out of the main process, which imports the same modules again
    later and cannot always survive it (#528).
    """
    # TODO: (#397) mutmut.workers.isolation imports POSIX-only modules
    from mutmut.workers.isolation import run_in_fork_with_result

    result: CoverageInfo = run_in_fork_with_result(_gather_coverage_in_this_process, runner, source_files)
    return result


def _gather_coverage_in_this_process(runner: TestRunner, source_files: Iterable[Path]) -> CoverageInfo:
    """The actual measurement. Pollutes the calling process; see gather_coverage."""
    mutants_path = Path("mutants")

    # Run the tests and gather coverage
    cov = coverage.Coverage(data_file=None)
    contexts = PerTestContexts(cov) if config().stats_from_coverage else None
    if contexts:
        exit_code = runner.collect_main_test_coverage(cov, plugins=[contexts])
    else:
        exit_code = runner.collect_main_test_coverage(cov)

    # Build mapping of filenames to covered lines
    # The CoverageData object is a wrapper around sqlite, and this
    # will make it more efficient to access the data
    info = CoverageInfo()
    coverage_data: CoverageData = cov.get_data()

    for filename in source_files:
        abs_filename = str((mutants_path / filename).absolute())
        info.covered_lines[abs_filename] = set(coverage_data.lines(abs_filename) or [])
        info.excluded_lines[abs_filename] = _excluded_lines(cov, abs_filename)
        if contexts:
            info.tests_by_mangled_function_name |= _tests_by_function(coverage_data, filename, abs_filename)

    if contexts:
        info.duration_by_test = contexts.duration_by_test
        info.test_exit_code = exit_code
    return info


class PerTestContexts:
    """Pytest plugin giving each test its own coverage context, switched where the stats run starts a test."""

    def __init__(self, cov: coverage.Coverage) -> None:
        self.cov = cov
        self.duration_by_test: dict[str, float] = {}

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        test = strip_prefix(nodeid, prefix="mutants/")
        self.duration_by_test[test] = 0.0
        self.cov.switch_context(test)

    def pytest_runtest_makereport(self, item: Item, call: CallInfo[None]) -> None:
        if call.when == "call":
            self.duration_by_test[strip_prefix(item.nodeid, prefix="mutants/")] += call.duration


def _tests_by_function(data: CoverageData, filename: Path, abs_filename: str) -> dict[str, set[str]]:
    """The tests that ran each function, read at the one line every call of it executes."""
    by_line = data.contexts_by_lineno(abs_filename)
    if not by_line:
        return {}
    try:
        entries = function_entry_lines(Path(abs_filename).read_text(), filename)
    except SyntaxError:
        return {}
    tests_by_function = {}
    for name, line in entries.items():
        if tests := {context for context in by_line.get(line, ()) if context}:
            tests_by_function[name] = tests
    return tests_by_function


def function_entry_lines(source: str, filename: Path) -> dict[str, int]:
    """First executed body line of each function mutmut trampolines, keyed like the stats."""
    lines: dict[str, int] = {}
    for node in ast.parse(source).body:
        members: list[tuple[str | None, ast.stmt]]
        match node:
            case ast.ClassDef(name=class_name, body=body):
                members = [(class_name, member) for member in body]
            case _:
                members = [(None, node)]
        for owner, member in members:
            match member:
                case ast.FunctionDef() | ast.AsyncFunctionDef():
                    key = get_mutant_name(filename, make_mutant_key(member.name, owner))
                    lines[key] = _entry_node(member.body).lineno
    return lines


def _entry_node(body: Sequence[ast.stmt]) -> ast.stmt | ast.expr:
    # A docstring or global declaration never executes, and coverage.py reports
    # a loop or branch at its test, so those are the lines to read.
    match body:
        case [ast.Expr(value=ast.Constant(value=str())) | ast.Global() | ast.Nonlocal(), *rest] if rest:
            return _entry_node(rest)
        case [ast.If(test=test) | ast.While(test=test), *_]:
            return test
        case [statement, *_]:
            return statement
    raise ValueError("function without a body")


# Asks coverage.py which lines of this file are excluded from measurement.
# This is analysis of the source, not of the collected data, so it is also
# correct for files the test run never imported.
def _excluded_lines(cov: coverage.Coverage, abs_filename: str) -> set[int]:
    try:
        _, _, excluded, _, _ = cov.analysis2(abs_filename)
    except CoverageException:
        # Unparseable or missing source: nothing we can say about it
        return set()

    return set(excluded)
