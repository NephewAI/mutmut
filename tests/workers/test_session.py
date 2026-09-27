import pytest

from mutmut.workers.session import _record
from mutmut.workers.session import shared_project_fixtures

pytest_plugins = ["pytester"]


def test_batch_progress_keeps_verdicts_and_leaves_interrupted_mutants_undecided():
    progress: dict[str, int | None] = {}
    for line in ["start a", "done a 1", "start b", "done b ", "start c", "done c 0", "start d"]:
        _record(line, progress)

    assert progress == {"a": 1, "b": None, "c": 0, "d": None}


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
