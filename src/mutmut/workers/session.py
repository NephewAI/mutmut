"""Test many mutants in one collected pytest session, instead of a process per mutant."""

from __future__ import annotations

import inspect
import io
import os
import select
import shutil
import tempfile
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
from dataclasses import field
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
    pending: Mapping[str, Sequence[str]], batch_size: int, workers: int, seconds_for: Mapping[str, float]
) -> dict[str, int]:
    """Verdicts for the mutants reused sessions could decide; the rest need an isolated run each.

    Up to ``workers`` sessions run side by side, each taking the next ``batch_size`` mutants.
    A session is stopped only when its current mutant outlives ``seconds_for`` that mutant.
    """
    decided: dict[str, int] = {}
    queue = list(pending)
    running: list[Batch] = []
    while queue or running:
        while queue and len(running) < workers:
            names, queue = queue[:batch_size], queue[batch_size:]
            running.append(Batch.start({name: pending[name] for name in names}, batch_size))
        timeout = max(0.0, min(batch.deadline for batch in running) - time.monotonic())
        ready, _, _ = select.select([batch.reader for batch in running], [], [], timeout)
        for batch in list(running):
            still_open = batch.read(seconds_for) if batch.reader in ready else True
            if still_open and time.monotonic() < batch.deadline:
                continue
            running.remove(batch)
            queue = batch.finish(decided, overran=still_open) + queue
    set_mutant_under_test("")
    return decided


# Seconds a session may take to collect its tests, or to move on to its next mutant.
STARTUP_SECONDS = 600.0


@dataclass
class Batch:
    """A forked session working through its share of the mutants, as the parent sees it."""

    pid: int
    reader: int
    names: list[str]
    temp_root: str
    deadline: float
    current: str | None = None
    buffer: bytes = b""
    progress: dict[str, int | None] = field(default_factory=dict)

    @classmethod
    def start(cls, pending: Mapping[str, Sequence[str]], batch_size: int) -> Batch:
        temp_root = tempfile.mkdtemp(prefix="mutmut-session-")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            # Sessions side by side must not prune each other's pytest temp directories.
            os.environ["PYTEST_DEBUG_TEMPROOT"] = temp_root
            writer = os.fdopen(write_fd, "w", buffering=1)
            try:
                run_batch(pending, batch_size, lambda line: print(line, file=writer, flush=True))
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(0)
        os.close(write_fd)
        return cls(pid, read_fd, list(pending), temp_root, time.monotonic() + STARTUP_SECONDS)

    def read(self, seconds_for: Mapping[str, float]) -> bool:
        """Take in the session's progress; False once it has exited."""
        chunk = os.read(self.reader, 65536)
        if not chunk:
            return False
        *lines, self.buffer = (self.buffer + chunk).split(b"\n")
        for line in lines:
            match line.decode().split(" "):
                case ["start", name]:
                    self.current, self.progress[name] = name, None
                    self.deadline = time.monotonic() + seconds_for[name]
                case ["done", name, verdict]:
                    self.current, self.progress[name] = None, int(verdict) if verdict else None
                    self.deadline = time.monotonic() + STARTUP_SECONDS
        return True

    def finish(self, decided: dict[str, int], *, overran: bool) -> list[str]:
        """Keep its verdicts; returns the mutants to retry, which never started because it stopped early."""
        if overran:
            os.kill(self.pid, 9)
        os.waitpid(self.pid, 0)
        os.close(self.reader)
        shutil.rmtree(self.temp_root, ignore_errors=True)
        decided.update({name: verdict for name, verdict in self.progress.items() if verdict is not None})
        # A mutant it was on when it crashed or overran gets an isolated run; ones it never reached go back in the queue.
        stopped_early = self.current is not None
        return [name for name in self.names if name not in self.progress] if stopped_early else []
