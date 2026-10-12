import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import mutmut
from mutmut.__main__ import _run
from mutmut.__main__ import cli
from mutmut.coverage_map import CoverageMap
from tests.e2e.e2e_utils import change_cwd
from tests.e2e.e2e_utils import forget_modules_from

MUTMUT_CONFIG = "[tool.mutmut]\nstats_from_coverage = true\nmutate_only_covered_lines = true\n"
FILES = {
    "src/covlib/__init__.py": "",
    "src/covlib/doubling.py": "def double(x):\n    return x * 2\n",
    "src/covlib/tripling.py": "def triple(x):\n    return x * 3\n",
    "tests/test_doubling.py": "from covlib.doubling import double\n\n\ndef test_double():\n    assert double(3) == 6\n",
    # Leaves a mark when imported, to show a skipped module is never imported.
    "tests/test_tripling.py": (
        "from pathlib import Path\n\nfrom covlib.tripling import triple\n\n"
        'Path(__file__).with_suffix(".imported").touch()\n\n\n'
        'def test_triple():\n    assert triple(3) == int(Path(__file__).with_name("factor.txt").read_text())\n'
    ),
    "tests/factor.txt": "9\n",
}


@pytest.fixture(autouse=True)
def isolated_imports(tmp_path):
    forget_modules_from(tmp_path)
    yield
    forget_modules_from(tmp_path)


def committed_project(root: Path, coverage_map: bool) -> Path:
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "pyproject.toml").write_text(MUTMUT_CONFIG + f"coverage_map = {str(coverage_map).lower()}\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=root, check=True)
    return root


def build_map(project: Path, *arguments: str) -> str:
    with change_cwd(project):
        mutmut._reset_globals()
        result = CliRunner().invoke(cli, ["coverage-map", *arguments])
    assert result.exit_code == 0, result.output
    forget_modules_from(project)
    return result.output


def diff_verdicts(project: Path, base: str = "HEAD") -> dict[str, int | None]:
    with change_cwd(project):
        mutmut._reset_globals()
        verdicts = _run([], None, diff_base=base)
    forget_modules_from(project)
    return {name: code for verdict in verdicts for name, code in verdict.exit_codes.items()}


def test_a_diff_run_runs_only_the_tests_that_reach_the_change_and_matches_a_full_run(tmp_path, capfd):
    mapped = committed_project(tmp_path / "mapped", coverage_map=True)
    plain = committed_project(tmp_path / "plain", coverage_map=False)
    build_map(mapped)
    imported_mark = mapped / "mutants" / "tests" / "test_tripling.imported"
    imported_mark.unlink()
    for project in (mapped, plain):
        (project / "src/covlib/doubling.py").write_text("def double(x):\n    return x + x\n")

    verdicts = diff_verdicts(mapped)

    assert "coverage map: running 1 of 2 known tests, for 1 changed files" in capfd.readouterr().out
    assert not imported_mark.exists()
    assert verdicts and verdicts == diff_verdicts(plain)
    assert set(verdicts.values()) == {1}


def test_a_changed_conftest_runs_every_test(tmp_path, capfd):
    project = committed_project(tmp_path / "mapped", coverage_map=True)
    build_map(project)
    (project / "tests" / "conftest.py").write_text("")
    (project / "src/covlib/doubling.py").write_text("def double(x):\n    return x + x\n")

    diff_verdicts(project)

    assert "coverage map: tests/conftest.py changed, running every test" in capfd.readouterr().out
    assert (project / "mutants" / "tests" / "test_tripling.imported").exists()


def test_the_map_records_each_test_s_files_and_each_module_import_s(tmp_path):
    project = committed_project(tmp_path / "mapped", coverage_map=True)

    build_map(project)

    coverage_map = CoverageMap.load(project / "mutants" / "mutmut-coverage-map.json")
    assert coverage_map is not None
    assert coverage_map.files_by_test["tests/test_doubling.py::test_double"] >= {
        "src/covlib/doubling.py",
        "tests/test_doubling.py",
    }
    assert "src/covlib/tripling.py" not in coverage_map.files_by_test["tests/test_doubling.py::test_double"]
    assert "src/covlib/tripling.py" in coverage_map.files_by_module["tests/test_tripling.py"]
    assert "tests/factor.txt" in coverage_map.files_by_test["tests/test_tripling.py::test_triple"]


def test_a_map_made_after_the_diff_base_still_runs_the_tests_of_everything_changed_since_it(tmp_path):
    mapped = committed_project(tmp_path / "mapped", coverage_map=True)
    plain = committed_project(tmp_path / "plain", coverage_map=False)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "double by adding"]
    for project in (mapped, plain):
        (project / "src/covlib/doubling.py").write_text("def double(x):\n    return x + x\n")
        subprocess.run(git, cwd=project, check=True)
    build_map(mapped)
    for project in (mapped, plain):
        (project / "src/covlib/tripling.py").write_text("def triple(x):\n    return x + x + x\n")

    verdicts = diff_verdicts(mapped, base="HEAD~1")

    assert any(name.startswith("covlib.doubling") for name in verdicts)
    assert verdicts == diff_verdicts(plain, base="HEAD~1")


def commit_all(project: Path, message: str) -> str:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A", "--", ":!mutants"], cwd=project, check=True)
    subprocess.run([*git, "commit", "-q", "-m", message], cwd=project, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip()


def test_an_update_reruns_only_the_tests_a_new_commit_can_reach_and_moves_the_map_to_it(tmp_path):
    project = committed_project(tmp_path / "mapped", coverage_map=True)
    build_map(project)
    imported_mark = project / "mutants" / "tests" / "test_tripling.imported"
    imported_mark.unlink()
    (project / "src/covlib/doubling.py").write_text("def double(x):\n    return x + x\n")
    head = commit_all(project, "double by adding")

    output = build_map(project)

    assert "coverage map: running 1 of 2 known tests, for 1 changed files; 1 ran" in output
    assert not imported_mark.exists()
    coverage_map = CoverageMap.load(project / "mutants" / "mutmut-coverage-map.json")
    assert coverage_map is not None
    assert coverage_map.commit == head
    assert set(coverage_map.files_by_test) == {
        "tests/test_doubling.py::test_double",
        "tests/test_tripling.py::test_triple",
    }


def test_a_full_rebuild_runs_every_test_and_forgets_ones_that_are_gone(tmp_path):
    project = committed_project(tmp_path / "mapped", coverage_map=True)
    build_map(project)
    (project / "tests" / "test_tripling.py").unlink()
    commit_all(project, "drop the tripling test")
    # Like CI: a fresh checkout with only the map restored, not the old mutants/ copy.
    map_path = project / "mutants" / "mutmut-coverage-map.json"
    saved = map_path.read_bytes()
    shutil.rmtree(project / "mutants")
    map_path.parent.mkdir()
    map_path.write_bytes(saved)

    output = build_map(project, "--full")

    assert "coverage map: running every test; 1 ran" in output
    coverage_map = CoverageMap.load(project / "mutants" / "mutmut-coverage-map.json")
    assert coverage_map is not None
    assert set(coverage_map.files_by_test) == {"tests/test_doubling.py::test_double"}
