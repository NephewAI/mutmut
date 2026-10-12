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


HANGING = {
    "src/hangs/__init__.py": (
        "def countdown(n):\n    while n > 0:\n        n -= 1\n    return n\n\n\n"
        "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n"
    ),
    "tests/test_hangs.py": (
        "from hangs import countdown, double, triple\n\n\n"
        "def test_countdown():\n    assert countdown(3) == 0\n\n\n"
        "def test_double():\n    assert double(3) == 6\n\n\n"
        "def test_triple():\n    assert triple(3) == 9\n"
    ),
}


def hanging_project(root, settings: str):
    for name, text in HANGING.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "pyproject.toml").write_text(f"[tool.mutmut]\ntimeout_multiplier = 2.0\n{settings}")
    return root


def test_a_mutant_that_hangs_stops_only_its_own_session_and_the_rest_are_still_decided(tmp_path, capfd):
    isolated = hanging_project(tmp_path / "isolated", "")
    reused = hanging_project(tmp_path / "reused", "session_batch_size = 32\n")

    expected = run_mutmut_in(isolated)
    forget_modules_from(isolated)
    capfd.readouterr()
    verdicts = run_mutmut_in(reused)

    assert verdicts == expected
    codes = list(expected["mutants/src/hangs/__init__.py.meta"].values())
    hangs = sum(code not in (0, 1) for code in codes)
    assert hangs  # some countdown mutants never end
    decided, pending = map(int, re.search(r"(\d+) of (\d+) mutants decided", capfd.readouterr().out).groups())
    assert decided == pending - hangs
