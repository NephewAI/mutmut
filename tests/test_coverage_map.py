import os
import subprocess

from coverage import CoverageData

from mutmut.coverage_map import CoverageMap
from mutmut.coverage_map import ProjectPaths
from mutmut.coverage_map import files_by_context

ROUTES = "src/app/routes.py"
MODELS = "src/app/models.py"
COVERAGE_MAP = CoverageMap(
    "abc123",
    {
        "tests/test_routes.py::test_get": frozenset({ROUTES, "tests/test_routes.py"}),
        "tests/test_routes.py::test_post": frozenset({"tests/test_routes.py"}),
        "tests/test_models.py::test_save": frozenset({MODELS, "tests/test_models.py"}),
    },
    {
        "tests/test_routes.py": frozenset({ROUTES, "tests/test_routes.py"}),
        "tests/test_models.py": frozenset({MODELS, "tests/test_models.py"}),
    },
)


def test_a_change_selects_the_tests_that_ran_it_and_every_test_whose_module_import_did():
    selection = COVERAGE_MAP.select({ROUTES}, rerun_all_patterns=[])

    assert not isinstance(selection, str)
    assert selection.tests == {"tests/test_routes.py::test_get", "tests/test_routes.py::test_post"}
    assert selection.modules == {"tests/test_routes.py"}
    assert selection.known_modules == {"tests/test_routes.py", "tests/test_models.py"}


def test_a_change_no_test_ran_selects_nothing():
    selection = COVERAGE_MAP.select({"src/app/unused.py", "README.md"}, rerun_all_patterns=[])

    assert not isinstance(selection, str)
    assert selection.tests == frozenset()


def test_a_change_to_test_configuration_runs_everything():
    assert COVERAGE_MAP.select({ROUTES, "tests/conftest.py"}, ["*conftest.py"]) == "tests/conftest.py changed"


def test_a_saved_map_loads_back_unchanged(tmp_path):
    path = tmp_path / "map.json"
    COVERAGE_MAP.save(path)

    assert CoverageMap.load(path) == COVERAGE_MAP


def test_a_missing_map_loads_as_none(tmp_path):
    assert CoverageMap.load(tmp_path / "map.json") is None


def test_merging_keeps_both_runs_files_for_each_test_at_the_newer_commit():
    newer = CoverageMap(
        "def456",
        {"tests/test_models.py::test_save": frozenset({"src/app/db.py"}), "tests/test_new.py::test_it": frozenset()},
        {},
    )

    merged = COVERAGE_MAP.merged(newer)

    assert merged.commit == "def456"
    assert merged.files_by_test["tests/test_models.py::test_save"] == {MODELS, "tests/test_models.py", "src/app/db.py"}
    assert merged.files_by_test["tests/test_new.py::test_it"] == frozenset()
    assert merged.files_by_module == COVERAGE_MAP.files_by_module


def test_a_measured_file_in_the_mutants_copy_reads_as_its_original(tmp_path):
    paths = ProjectPaths(repo=tmp_path, project=tmp_path / "packages" / "app")

    assert paths.in_repo(str(paths.project / "mutants" / ROUTES)) == f"packages/app/{ROUTES}"
    assert paths.in_repo(str(tmp_path / "packages" / "lib" / "src" / "lib.py")) == "packages/lib/src/lib.py"
    assert paths.in_repo("/usr/lib/python3/json/__init__.py") is None
    assert paths.in_project(f"packages/app/{ROUTES}") == ROUTES
    assert paths.in_project("packages/lib/src/lib.py") is None


def test_only_the_project_s_mutants_copy_is_left_out_of_the_changes(tmp_path):
    project = tmp_path / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("x = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (project / "src" / "app.py").write_text("x = 2\n")
    (project / "src" / "new.py").write_text("y = 1\n")
    (project / "mutants" / "src").mkdir(parents=True)
    (project / "mutants" / "src" / "app.py").write_text("x = 2\n")

    paths = ProjectPaths(repo=tmp_path.resolve(), project=project.resolve())

    assert paths.changed_since(head) == {"app/src/app.py", "app/src/new.py"}
    assert paths.head() is None
    assert paths.changed_since("not-a-commit") is None


def test_a_pyproject_change_counts_only_when_it_can_change_what_tests_do(tmp_path):
    allowlist = '[tool.mutmut]\npytest_add_cli_args_test_selection = ["tests/test_a.py"]\n'
    for name in ("listed", "linted", "tested"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text(allowlist)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True)
    (tmp_path / "listed" / "pyproject.toml").write_text(allowlist.replace('"]', '", "tests/test_b.py"]'))
    (tmp_path / "linted" / "pyproject.toml").write_text(allowlist + "[tool.vulture]\nignore_names = ['x']\n")
    (tmp_path / "tested" / "pyproject.toml").write_text(allowlist + "[tool.pytest.ini_options]\naddopts = '-p x'\n")

    paths = ProjectPaths(repo=tmp_path.resolve(), project=tmp_path.resolve())

    assert paths.changed_since("HEAD") == {"tested/pyproject.toml"}


def test_a_file_imported_through_a_mount_of_the_repository_reads_as_the_repository_s(tmp_path):
    repo, mount = tmp_path / "repo", tmp_path / "app"
    source = repo / "runtime" / "packages" / "lib" / "src" / "lib.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n")
    (mount / "packages" / "lib" / "src").mkdir(parents=True)
    os.link(source, mount / "packages" / "lib" / "src" / "lib.py")  # a bind mount keeps the inode too
    (tmp_path / "elsewhere.py").write_text("y = 1\n")
    paths = ProjectPaths(repo=repo, project=repo / "runtime" / "packages" / "app")

    assert paths.in_repo(str(mount / "packages" / "lib" / "src" / "lib.py")) == "runtime/packages/lib/src/lib.py"
    assert paths.mounts == {mount: repo / "runtime"}
    assert paths.in_repo(str(tmp_path / "elsewhere.py")) is None


def test_each_context_gets_the_files_it_ran_a_line_of(tmp_path):
    data = CoverageData(no_disk=True)
    for context, files in {"t1": ["a.py", "b.py"], "t2": ["b.py"], "": ["c.py"]}.items():
        data.set_context(context)
        data.add_lines({str(tmp_path / name): {1, 2} for name in files})
    paths = ProjectPaths(repo=tmp_path, project=tmp_path)

    assert files_by_context(data, paths) == {"t1": {"a.py", "b.py"}, "t2": {"b.py"}, "": {"c.py"}}
