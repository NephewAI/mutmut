import re
import shutil

import pytest

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


def test_reused_sessions_give_the_same_verdicts_as_a_process_per_mutant(tmp_path, capfd):
    isolated, reused = tmp_path / "isolated", tmp_path / "reused"
    for project in (isolated, reused):
        shutil.copytree(E2E_PROJECTS / "my_lib", project, ignore=shutil.ignore_patterns("mutants"))
    pyproject = reused / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("[tool.mutmut]\n", "[tool.mutmut]\nsession_batch_size = 32\n"))

    expected = run_mutmut_in(isolated)
    forget_modules_from(isolated)
    capfd.readouterr()
    verdicts = run_mutmut_in(reused)

    assert verdicts == expected
    decided, pending = map(int, re.search(r"(\d+) of (\d+) mutants decided", capfd.readouterr().out).groups())
    assert decided > pending / 2
    assert -11 in expected["mutants/src/my_lib/__init__.py.meta"].values()  # a mutant that crashes its batch
