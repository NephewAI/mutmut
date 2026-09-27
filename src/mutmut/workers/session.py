"""Test many mutants in one collected pytest session, instead of a process per mutant."""

from __future__ import annotations

import inspect
import io
import os
import select
import time
import traceback
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mutmut.configuration import config
from mutmut.mutation.trampoline import set_mutant_under_test
from mutmut.utils.file_utils import change_cwd
from mutmut.utils.format_utils import strip_prefix

if TYPE_CHECKING:
    from _pytest.config import Config as PytestConfig
    from _pytest.main import Session
    from _pytest.nodes import Item

KILLED, SURVIVED = 1, 0


@dataclass(frozen=True)
class ReusedSession:
    """A collected pytest session that runs one mutant after another without re-importing."""

    config: PytestConfig
    session: Session
    items: Mapping[str, Item]

    @classmethod
    def collect(cls, tests: Iterable[str]) -> ReusedSession:
        from _pytest.config import _prepareconfig
        from _pytest.main import Session

        arguments = ["--rootdir=.", "--tb=native", "-x", "-q", *config().pytest_add_cli_args, *sorted(tests)]
        with change_cwd("mutants"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pytest_config = _prepareconfig(arguments)
            pytest_config._do_configure()
            session = Session.from_config(pytest_config)
            pytest_config.hook.pytest_sessionstart(session=session)
            pytest_config.hook.pytest_collection(session=session)
        if session.testsfailed:
            raise RuntimeError("pytest collection failed")
        items = {strip_prefix(item.nodeid, prefix="mutants/"): item for item in session.items}
        return cls(pytest_config, session, items)

    def run_mutant(self, name: str, tests: Sequence[str]) -> int:
        """Killed if any test fails, else survived."""
        from _pytest.main import Failed

        set_mutant_under_test(name)
        session = self.session
        session.items = [self.items[test] for test in tests]
        session.testscollected, session.testsfailed = len(session.items), 0
        # The public setters refuse to reset a stop once a test failed under -x.
        session._shouldfail = session._shouldstop = False
        with change_cwd("mutants"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), suppress(Failed):
            self.config.hook.pytest_runtestloop(session=session)
        return KILLED if session.testsfailed else SURVIVED

    def runnable_tests(self, project_root: Path) -> set[str]:
        """Tests whose broader-than-function fixtures all come from outside the project, so no mutant can leak into them."""
        return {test for test, item in self.items.items() if not shared_project_fixtures(item, project_root)}


def shared_project_fixtures(item: Item, project_root: Path) -> list[str]:
    """Fixtures of this test that outlive it and are defined in the project."""
    fixture_info = getattr(item, "_fixtureinfo", None)
    if fixture_info is None:
        return [type(item).__name__]
    return [
        name
        for name, definitions in fixture_info.name2fixturedefs.items()
        if definitions and str(definitions[-1].scope) != "function" and defined_in(definitions[-1].func, project_root)
    ]


def defined_in(func: Callable[..., object], root: Path) -> bool:
    try:
        return Path(inspect.getfile(func)).resolve().is_relative_to(root)
    except TypeError:
        return True  # can't locate its source, so don't trust it


def run_batch(pending: Mapping[str, Sequence[str]], batch_size: int, report: Callable[[str], None]) -> None:
    """Test up to ``batch_size`` mutants in one session, reporting each start and verdict as it happens."""
    session = ReusedSession.collect({test for tests in pending.values() for test in tests})
    runnable = session.runnable_tests(Path.cwd().resolve())
    batch = [(name, tests) for name, tests in pending.items() if any(test in runnable for test in tests)]
    for name, tests in batch[:batch_size]:
        eligible = [test for test in tests if test in runnable]
        report(f"start {name}")
        verdict = session.run_mutant(name, eligible)
        # A survivor counts only if every covering test ran; otherwise an isolated run decides.
        report(f"done {name} {verdict if verdict == KILLED or len(eligible) == len(tests) else ''}")


def run_in_reused_sessions(
    pending: Mapping[str, Sequence[str]], batch_size: int, batch_seconds: float
) -> dict[str, int]:
    """Verdicts for the mutants reused sessions could decide; the rest need an isolated run each."""
    decided: dict[str, int] = {}
    attempted: set[str] = set()
    while todo := {name: tests for name, tests in pending.items() if name not in attempted}:
        progress = _run_batch_in_child(todo, batch_size, batch_seconds)
        if not progress:
            break
        for name, verdict in progress:
            attempted.add(name)
            if verdict is not None:
                decided[name] = verdict
    return decided


def _run_batch_in_child(
    pending: Mapping[str, Sequence[str]], batch_size: int, batch_seconds: float
) -> list[tuple[str, int | None]]:
    """Fork a batch and read its progress until it finishes, crashes or runs out of time."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        writer = os.fdopen(write_fd, "w", buffering=1)
        try:
            run_batch(pending, batch_size, lambda line: print(line, file=writer, flush=True))
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(0)
    os.close(write_fd)
    progress: dict[str, int | None] = {}
    deadline = time.monotonic() + batch_seconds
    buffer = b""
    with os.fdopen(read_fd, "rb") as reader:
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([reader], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(reader.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                _record(line.decode(), progress)
        else:
            os.kill(pid, 9)
    os.waitpid(pid, 0)
    set_mutant_under_test("")
    return list(progress.items())


def _record(line: str, progress: dict[str, int | None]) -> None:
    match line.split(" "):
        case ["start", name]:
            progress[name] = None
        case ["done", name, verdict]:
            progress[name] = int(verdict) if verdict else None
