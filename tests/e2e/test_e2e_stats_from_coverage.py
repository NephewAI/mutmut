import shutil
import subprocess
import sys

import pytest

from mutmut.state import state
from tests.e2e.e2e_utils import E2E_PROJECTS
from tests.e2e.e2e_utils import forget_modules_from
from tests.e2e.e2e_utils import run_mutmut_in


@pytest.fixture(autouse=True)
def isolated_imports(tmp_path):
    """Neither reuse nor leave behind modules imported from another copy of the project."""
    forget_modules_from(E2E_PROJECTS)
    forget_modules_from(tmp_path.parent)
    yield
    forget_modules_from(tmp_path.parent)


def test_stats_from_coverage_gives_the_same_verdicts_as_the_stats_run(tmp_path):
    stats_run, from_coverage = tmp_path / "stats_run", tmp_path / "from_coverage"
    for project in (stats_run, from_coverage):
        shutil.copytree(E2E_PROJECTS / "my_lib", project, ignore=shutil.ignore_patterns("mutants"))
    pyproject = from_coverage / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace("[tool.mutmut]\n", "[tool.mutmut]\nstats_from_coverage = true\n")
    )

    expected = run_mutmut_in(stats_run)
    assert state()._coverage_stats is None
    forget_modules_from(stats_run)
    verdicts = run_mutmut_in(from_coverage)

    assert state()._coverage_stats is not None
    assert verdicts == expected
    assert len(expected["mutants/src/my_lib/__init__.py.meta"]) > 50


def test_a_failing_test_stops_the_run_before_any_mutant_and_shows_why(tmp_path):
    project = tmp_path / "broken"
    shutil.copytree(E2E_PROJECTS / "my_lib", project, ignore=shutil.ignore_patterns("mutants"))
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace("[tool.mutmut]\n", "[tool.mutmut]\nstats_from_coverage = true\n")
    )
    tests = project / "tests" / "test_my_lib.py"
    tests.write_text(tests.read_text() + "\n\ndef test_broken_on_purpose():\n    assert hello() == 'something else'\n")

    result = subprocess.run([sys.executable, "-m", "mutmut", "run"], cwd=project, capture_output=True, text=True)

    assert result.returncode == 1
    assert "test_broken_on_purpose" in result.stdout
    assert "the tests fail without any mutant" in result.stdout
    assert not list((project / "mutants").rglob("*.meta"))
