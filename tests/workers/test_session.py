import os

import pytest

from mutmut.workers.session import Batch
from mutmut.workers.session import shared_project_fixtures

pytest_plugins = ["pytester"]


def finished_batch(tmp_path, output: bytes) -> Batch:
    """A batch whose session already exited after writing this progress."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    (tmp_path / "temp").mkdir()
    os.write(write_fd, output)
    os.close(write_fd)
    batch = Batch(pid, read_fd, ["a", "b", "c", "d", "e"], str(tmp_path / "temp"), deadline=0.0)
    batch.read(dict.fromkeys("abcde", 1.0))
    return batch


def test_a_session_stopped_mid_mutant_keeps_its_verdicts_and_returns_the_mutants_it_never_reached(tmp_path):
    batch = finished_batch(tmp_path, b"start a\ndone a 1\nstart b\ndone b \nstart c\ndone c 0\nstart d\n")
    decided: dict[str, int] = {}

    assert batch.finish(decided, overran=False) == ["e"]
    assert decided == {"a": 1, "c": 0}
    assert not (tmp_path / "temp").exists()


def test_a_session_that_finished_leaves_the_mutants_it_skipped_to_isolated_runs(tmp_path):
    batch = finished_batch(tmp_path, b"start a\ndone a 1\nstart b\ndone b 0\n")
    decided: dict[str, int] = {}

    assert batch.finish(decided, overran=False) == []
    assert decided == {"a": 1, "b": 0}


def test_reused_sessions_skip_tests_whose_broader_fixtures_come_from_the_project(pytester: pytest.Pytester):
    items = pytester.getitems(
        """
        import pytest

        @pytest.fixture
        def fresh():
            return 1

        @pytest.fixture(scope="module")
        def shared():
            return 1

        def test_fresh(fresh, tmp_path_factory):
            assert fresh

        def test_shared(shared):
            assert shared
        """
    )
    root = pytester.path.resolve()

    blockers = {item.name: shared_project_fixtures(item, root) for item in items}

    assert blockers == {"test_fresh": [], "test_shared": ["shared"]}
