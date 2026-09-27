import shutil
import sys

from mutmut.state import state
from tests.e2e.e2e_utils import E2E_PROJECTS
from tests.e2e.e2e_utils import run_mutmut_in


def forget_modules_from(directory):
    """Coverage keys on file paths, so modules a copy imported earlier in this process must not be reused."""
    for name, module in list(sys.modules.items()):
        if str(getattr(module, "__file__", None) or "").startswith(str(directory)):
            del sys.modules[name]


def test_stats_from_coverage_gives_the_same_verdicts_as_the_stats_run(tmp_path):
    stats_run, from_coverage = tmp_path / "stats_run", tmp_path / "from_coverage"
    for project in (stats_run, from_coverage):
        shutil.copytree(E2E_PROJECTS / "my_lib", project, ignore=shutil.ignore_patterns("mutants"))
    pyproject = from_coverage / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace("[tool.mutmut]\n", "[tool.mutmut]\nstats_from_coverage = true\n")
    )

    forget_modules_from(E2E_PROJECTS)
    expected = run_mutmut_in(stats_run)
    assert state()._coverage_stats is None
    forget_modules_from(stats_run)
    verdicts = run_mutmut_in(from_coverage)

    assert state()._coverage_stats is not None
    assert verdicts == expected
    assert len(expected["mutants/src/my_lib/__init__.py.meta"]) > 50
