import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import mutmut
from mutmut.__main__ import _run
from mutmut.__main__ import cli
from tests.e2e.e2e_utils import E2E_PROJECTS
from tests.e2e.e2e_utils import change_cwd
from tests.e2e.e2e_utils import forget_modules_from

SOURCE = Path("src/my_lib/__init__.py")
TESTS = Path("tests/test_my_lib.py")


@pytest.fixture(autouse=True)
def isolated_imports(tmp_path):
    """Neither reuse nor leave behind modules imported from another copy of the project."""
    forget_modules_from(E2E_PROJECTS)
    forget_modules_from(tmp_path.parent)
    yield
    forget_modules_from(tmp_path.parent)


def committed_copy(tmp_path: Path, extra_config: str) -> Path:
    project = tmp_path / "my_lib"
    shutil.copytree(E2E_PROJECTS / "my_lib", project, ignore=shutil.ignore_patterns("mutants"))
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("[tool.mutmut]\n", f"[tool.mutmut]\n{extra_config}"))
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=project, check=True)
    return project


def run_diff(project: Path) -> list:
    with change_cwd(project):
        mutmut._reset_globals()
        return _run([], None, diff_base="HEAD")


def append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


@pytest.mark.parametrize(
    "extra_config",
    ["", "stats_from_coverage = true\nmutate_only_covered_lines = true\n"],
    ids=["stats-run", "stats-from-coverage"],
)
def test_a_diff_run_mutates_only_the_changed_function_and_passes_when_its_tests_kill_everything(tmp_path, extra_config):
    project = committed_copy(tmp_path, extra_config)
    append(project / SOURCE, "\n\ndef doubled(x):\n    return x * 2\n")
    append(project / TESTS, "\n\ndef test_doubled():\n    assert doubled(3) == 6\n")

    (verdict,) = run_diff(project)

    assert verdict.path == str(SOURCE)
    assert verdict.exit_codes and all(name.startswith("my_lib.x_doubled__mutmut_") for name in verdict.exit_codes)
    assert verdict.unresolved == []


def test_a_diff_run_fails_when_a_changed_line_has_surviving_mutants(tmp_path):
    project = committed_copy(tmp_path, "stats_from_coverage = true\nmutate_only_covered_lines = true\n")
    source = project / SOURCE
    source.write_text(source.read_text().replace('"Mutants for this method should survive"', '"These should survive"'))

    (verdict,) = run_diff(project)

    assert (verdict.changed, verdict.covered) == (2, 1)  # the string also sits on an untested line
    assert verdict.unresolved
    assert all(name.startswith("my_lib.x_badly_tested__mutmut_") for name in verdict.exit_codes)


def test_a_diff_run_stops_before_testing_when_no_changed_line_can_be_mutated(tmp_path, capsys):
    project = committed_copy(tmp_path, "stats_from_coverage = true\n")
    append(project / SOURCE, "\n# a comment\n")

    assert run_diff(project) == []
    assert "No mutants possible" in capsys.readouterr().out
    assert not (project / "mutants").exists()


def test_the_run_command_exits_non_zero_when_a_changed_line_has_surviving_mutants(tmp_path, capfd):
    project = committed_copy(tmp_path, "stats_from_coverage = true\nmutate_only_covered_lines = true\n")
    source = project / SOURCE
    source.write_text(source.read_text().replace('"Mutants for this method should survive"', '"These should survive"'))

    with change_cwd(project):
        mutmut._reset_globals()
        result = CliRunner().invoke(cli, ["run", "--diff-base", "HEAD"])

    assert result.exit_code == 1
    # mutmut writes to the real stdout, past the click runner's capture.
    assert "not killed: my_lib.x_badly_tested__mutmut_1 (survived)" in capfd.readouterr().out


def test_shards_of_a_diff_run_split_its_mutants_without_overlap(tmp_path):
    project = committed_copy(tmp_path, "stats_from_coverage = true\nmutate_only_covered_lines = true\n")
    append(project / SOURCE, "\n\ndef doubled(x):\n    return x * 2\n\n\ndef tripled(x):\n    return x * 3\n")
    append(project / TESTS, "\n\ndef test_multiples():\n    assert doubled(3) == 6\n    assert tripled(3) == 9\n")

    def mutants(shard: tuple[int, int] | None) -> set[str]:
        forget_modules_from(project)  # each CLI run is a fresh process
        with change_cwd(project):
            mutmut._reset_globals()
            verdicts = _run([], None, diff_base="HEAD", shard=shard)
        return {name for verdict in verdicts for name in verdict.exit_codes}

    whole = mutants(None)
    parts = [mutants((index, 4)) for index in range(4)]

    assert whole and set().union(*parts) == whole
    assert sum(len(part) for part in parts) == len(whole)


def test_shard_needs_a_diff_base():
    result = CliRunner().invoke(cli, ["run", "--shard", "0/4"])

    assert result.exit_code == 2
    assert "--shard needs --diff-base" in result.output
