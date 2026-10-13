import subprocess
from pathlib import Path

from mutmut.coverage_invalidation import content_aware_reason
from mutmut.coverage_invalidation import lock_change
from mutmut.coverage_invalidation import pyproject_change
from mutmut.coverage_invalidation import used_distributions
from mutmut.coverage_map import CoverageMap
from mutmut.coverage_map import ProjectPaths
from mutmut.workspace import parse_toml

DEFAULTS = ["*pyproject.toml", "*.lock"]
PYTEST = "[tool.pytest.ini_options]\naddopts = '-p x'\n"
DEPENDENCIES = '[project]\nname = "app"\ndependencies = ["requests"]\n'


DEPENDS = {
    "requests": 'dependencies = [{ name = "idna" }]\n',
    "pytest-xdist": 'dependencies = [{ name = "pytest" }]\n',
}


def lock(versions: dict[str, str] | None = None, extra_group: str = "pytest-xdist") -> str:
    """A workspace lock: root dev group (pytest, pytest-xdist), member `app` -> requests -> idna, and `other`."""
    versions = {"pytest": "9.0", "pytest-xdist": "3.8", "requests": "2.32", "idna": "3.10", "unrelated": "1.0"} | (
        versions or {}
    )
    packages = "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        f"{DEPENDS.get(name, '')}\n"
        for name, version in versions.items()
    )
    return f"""version = 1
revision = 3

[manifest]
members = ["app", "other"]

[manifest.dependency-groups]
dev = [{{ name = "pytest", specifier = ">=9" }}, {{ name = "{extra_group}", specifier = ">=3" }}]

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "packages/app" }}
dependencies = [{{ name = "requests" }}]

[package.optional-dependencies]
fast = [{{ name = "ujson" }}]

[[package]]
name = "other"
version = "0.1.0"
source = {{ editable = "packages/other" }}
dependencies = [{{ name = "unrelated" }}]

[[package]]
name = "ujson"
version = "5.0"
source = {{ registry = "https://pypi.org/simple" }}

{packages}"""


def test_the_pytest_table_changing_runs_everything():
    old, new = DEPENDENCIES + PYTEST, DEPENDENCIES + PYTEST.replace("-p x", "-p y")

    assert pyproject_change(old, new, has_lock=True) == "[tool.pytest.ini_options] changed"


def test_a_coverage_or_mutmut_table_changing_runs_everything():
    assert pyproject_change(DEPENDENCIES, DEPENDENCIES + "[tool.coverage.run]\nbranch = true\n", True) == (
        "[tool.coverage.run] changed"
    )
    assert pyproject_change(DEPENDENCIES, DEPENDENCIES + "[tool.mutmut]\ncoverage_map = true\n", True) == (
        "[tool.mutmut] changed"
    )


def test_tests_listed_for_mutmut_and_linter_settings_do_not_run_everything():
    old = DEPENDENCIES + '[tool.mutmut]\npytest_add_cli_args_test_selection = ["tests/a.py"]\n'
    new = old.replace('"tests/a.py"', '"tests/a.py", "tests/b.py"') + "[tool.ruff]\nline-length = 100\n"

    assert pyproject_change(old, new, has_lock=False) is None


def test_a_dependency_list_changing_does_not_run_everything_when_a_lock_covers_it():
    new = DEPENDENCIES.replace('["requests"]', '["requests", "attrs"]')

    assert pyproject_change(DEPENDENCIES, new, has_lock=True) is None


def test_a_dependency_list_changing_runs_everything_without_a_lock():
    new = DEPENDENCIES.replace('["requests"]', '["requests", "attrs"]')

    assert pyproject_change(DEPENDENCIES, new, has_lock=False) == "[project.dependencies] changed"
    assert pyproject_change(DEPENDENCIES, DEPENDENCIES + "[dependency-groups]\ndev = ['x']\n", False) == (
        "[dependency-groups] changed"
    )


def test_the_project_uses_its_transitive_requirements_extras_and_the_roots_groups():
    used = used_distributions(parse_toml(lock()), "app")

    assert used == {"app", "requests", "idna", "ujson", "pytest", "pytest-xdist"}


def test_a_lock_change_to_an_unrelated_distribution_does_not_run_everything():
    assert lock_change(lock(), lock({"unrelated": "2.0"}), "app", governing=True) is None


def test_a_lock_change_to_a_transitive_dependency_runs_everything():
    reason = lock_change(lock(), lock({"idna": "3.11"}), "app", governing=True)

    assert reason == "changed idna used by app"


def test_a_lock_change_to_a_root_dev_group_dependency_runs_everything():
    assert lock_change(lock(), lock({"pytest": "9.1"}), "app", governing=True) == "changed pytest used by app"


def test_a_distribution_joining_the_root_dev_group_runs_everything():
    reason = lock_change(lock(), lock(extra_group="unrelated"), "app", governing=True)

    assert reason == "changed pytest-xdist, unrelated used by app"


def test_a_removed_distribution_the_old_lock_used_runs_everything():
    shrunk = lock().replace('dependencies = [{ name = "requests" }]', "dependencies = []", 1)

    assert lock_change(lock(), shrunk, "app", governing=True) == "changed idna, requests used by app"


def test_a_lock_that_cannot_be_judged_is_undecidable():
    for old, new, project, governing in (
        (lock(), lock().replace("version = 1", "version = 2", 1), "app", True),
        (lock(), "not [ toml", "app", True),
        (lock(), lock(), None, True),
        (lock(), lock(), "app", False),
        (lock(), lock(), "missing", True),
    ):
        try:
            lock_change(old, new, project, governing)
        except ValueError:
            continue
        raise AssertionError(f"judged {project} {governing}")


def repository(tmp_path: Path, files: dict[str, str]) -> ProjectPaths:
    """A git repo with ``files`` committed, and the project at runtime/packages/app."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True)
    return ProjectPaths(repo=tmp_path.resolve(), project=(tmp_path / "runtime/packages/app").resolve())


APP = "runtime/packages/app/pyproject.toml"
LOCK = "runtime/uv.lock"
FILES = {APP: DEPENDENCIES + PYTEST, LOCK: lock(), "runtime/poetry.lock": "a\n"}


def reason(paths: ProjectPaths, *changed: str, patterns=DEFAULTS) -> tuple[str | None, set[str]]:
    return content_aware_reason(paths, "HEAD", changed, patterns)


def test_a_changed_pytest_table_names_the_file_and_table(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / APP).write_text(DEPENDENCIES + PYTEST.replace("-p x", "-p y"))

    assert reason(paths, APP) == (f"{APP} [tool.pytest.ini_options] changed", {APP})


def test_a_changed_dependency_list_with_a_lock_above_is_settled_without_a_rerun(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / APP).write_text(DEPENDENCIES.replace('["requests"]', '["requests", "attrs"]') + PYTEST)

    assert reason(paths, APP) == (None, {APP})


def test_a_changed_dependency_list_without_a_lock_runs_everything(tmp_path):
    paths = repository(tmp_path, {APP: DEPENDENCIES})
    (tmp_path / APP).write_text(DEPENDENCIES.replace('["requests"]', '["requests", "attrs"]'))

    assert reason(paths, APP)[0] == f"{APP} [project.dependencies] changed"


def test_a_lock_change_for_an_unrelated_distribution_is_settled_without_a_rerun(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"unrelated": "2.0"}))

    assert reason(paths, LOCK) == (None, {LOCK})


def test_a_lock_change_for_a_transitive_dependency_names_the_distribution_and_project(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"idna": "3.11"}))

    assert reason(paths, LOCK)[0] == f"{LOCK} changed idna used by app"


def test_a_lock_change_for_a_root_dev_group_dependency_runs_everything(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"pytest-xdist": "3.9"}))

    assert reason(paths, LOCK)[0] == f"{LOCK} changed pytest-xdist used by app"


def test_old_contents_that_are_unavailable_run_everything(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / "runtime/new/pyproject.toml").parent.mkdir()
    (tmp_path / "runtime/new/pyproject.toml").write_text(PYTEST)

    absent, _ = reason(paths, "runtime/new/pyproject.toml")
    unknown_commit, _ = content_aware_reason(paths, "not-a-commit", [APP], DEFAULTS)

    assert absent == "runtime/new/pyproject.toml changed (could not compare it with the old one)"
    assert unknown_commit == f"{APP} changed (could not compare it with the old one)"


def test_malformed_toml_runs_everything(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / APP).write_text("[tool.pytest\n")

    assert reason(paths, APP)[0] == f"{APP} changed (could not compare it with the old one)"


def test_a_lock_that_does_not_sit_above_the_project_runs_everything(tmp_path):
    paths = repository(tmp_path, {**FILES, "skills/uv.lock": lock()})
    (tmp_path / "skills/uv.lock").write_text(lock({"idna": "3.11"}))

    assert reason(paths, "skills/uv.lock")[0] == "skills/uv.lock changed (could not compare it with the old one)"


def test_other_lock_files_keep_the_blanket_rule(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / "runtime/poetry.lock").write_text("b\n")
    changed = {"runtime/poetry.lock", "src/app.py"}

    judged, settled = reason(paths, *changed)
    selection = CoverageMap("abc", {}, {}).select(changed, DEFAULTS, settled)

    assert (judged, settled) == (None, set())
    assert selection == "runtime/poetry.lock changed"


def test_a_user_pattern_that_matches_a_pyproject_or_lock_still_runs_everything(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"unrelated": "2.0"}))
    patterns = [*DEFAULTS, "runtime/uv.lock"]

    judged, settled = reason(paths, LOCK, patterns=patterns)

    assert (judged, settled) == (None, set())
    assert CoverageMap("abc", {}, {}).select({LOCK}, patterns, settled) == f"{LOCK} changed"


def test_a_user_pattern_alone_keeps_other_files_blanket_beside_settled_ones(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"unrelated": "2.0"}))
    patterns = [*DEFAULTS, "*conftest.py"]
    changed = {LOCK, "tests/conftest.py"}

    _, settled = reason(paths, *changed, patterns=patterns)

    assert CoverageMap("abc", {}, {}).select(changed, patterns, settled) == "tests/conftest.py changed"


def test_a_settled_lock_change_leaves_the_map_to_pick_tests(tmp_path):
    paths = repository(tmp_path, FILES)
    (tmp_path / LOCK).write_text(lock({"unrelated": "2.0"}))
    _, settled = reason(paths, LOCK)

    selection = CoverageMap("abc", {"t.py::a": frozenset({"x.py"})}, {}).select({LOCK}, DEFAULTS, settled)

    assert not isinstance(selection, str)
    assert selection.tests == frozenset()
