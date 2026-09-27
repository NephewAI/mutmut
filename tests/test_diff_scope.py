import subprocess
from pathlib import Path

import click
import pytest

from mutmut.__main__ import parse_shard
from mutmut.diff_scope import DiffScope
from mutmut.diff_scope import FileVerdict
from mutmut.diff_scope import parse_changed_lines
from mutmut.diff_scope import split_by_function

DIFF = """\
diff --git src/a.py src/a.py
--- src/a.py
+++ src/a.py
@@ -2 +2,2 @@ def f():
-    return 1
+    x = 2
+    return x
@@ -9,3 +10,0 @@ def g():
-    pass
-    pass
-    pass
@@ -20 +18 @@ def h():
-    return 3
+    return 4
diff --git src/new.py src/new.py
new file mode 100644
--- /dev/null
+++ src/new.py
@@ -0,0 +1,3 @@
+def added():
+    return 1
+
diff --git src/gone.py src/gone.py
deleted file mode 100644
--- src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    return 1
diff --git src/old.py src/renamed.py
similarity index 100%
rename from src/old.py
rename to src/renamed.py
"""


def test_changed_lines_are_the_new_side_of_every_hunk():
    assert parse_changed_lines(DIFF) == {
        "src/a.py": frozenset({2, 3, 18}),
        "src/new.py": frozenset({1, 2, 3}),
    }


def test_line_filter_intersects_changed_lines_with_coverage_only_when_it_is_measured():
    scope = DiffScope("main", {"src/a.py": frozenset({2, 3})})
    key = str((Path("mutants") / "src/a.py").absolute())

    assert scope.line_filter(None) == {key: {2, 3}}
    assert scope.line_filter({key: {3, 9}}) == {key: {3}}
    assert scope.line_filter({}) == {key: set()}


def test_mutants_are_possible_only_where_the_changed_lines_allow_them(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text('"""Doc."""\n\nimport os\n\n\ndef total(x):\n    return x + 1\n')
    monkeypatch.chdir(tmp_path)

    assert not DiffScope("main", {"src/a.py": frozenset({1, 3, 6})}).has_possible_mutants()
    assert DiffScope("main", {"src/a.py": frozenset({7})}).has_possible_mutants()


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def test_scope_reads_committed_staged_and_unstaged_changes_in_source_files_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "src/b.py").write_text("c = 3\n")
    (tmp_path / "README.md").write_text("docs\n")
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "src/a.py").write_text("a = 1\nb = 20\n")
    git("commit", "-q", "-am", "committed")
    (tmp_path / "src/b.py").write_text("c = 3\nd = 4\n")
    git("add", "src/b.py")
    (tmp_path / "src/a.py").write_text("a = 10\nb = 20\n")
    (tmp_path / "README.md").write_text("more docs\n")

    scope = DiffScope.from_git(base, [Path("src/a.py"), Path("src/b.py")])

    assert scope.changed_lines == {"src/a.py": frozenset({1, 2}), "src/b.py": frozenset({2})}


def test_forgetting_previous_mutants_removes_only_the_changed_files_mutants(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("mutants/src/a.py", "mutants/src/a.py.meta", "mutants/src/b.py", "mutants/src/b.py.meta"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")

    DiffScope("main", {"src/a.py": frozenset({1})}).forget_previous_mutants()

    assert sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "mutants/src").iterdir()) == [
        "mutants/src/b.py",
        "mutants/src/b.py.meta",
    ]


def test_a_file_passes_only_when_every_mutant_is_killed_or_caught_by_the_type_checker():
    verdict = FileVerdict(
        "src/a.py", 3, 2, {"a.x_f__mutmut_1": 1, "a.x_f__mutmut_2": 37, "a.x_f__mutmut_3": 0, "a.x_f__mutmut_4": 33}
    )

    assert verdict.unresolved == ["a.x_f__mutmut_3 (survived)", "a.x_f__mutmut_4 (no tests)"]
    assert verdict.describe() == "src/a.py: changed=3 covered=2 mutants=4 resolved=2"
    assert FileVerdict("src/a.py", 1, None, {"a.x_f__mutmut_1": None}).unresolved == ["a.x_f__mutmut_1 (not checked)"]


SHARDED_SOURCE = "module = 1\n\ndef first():\n    one = 1\n    two = 2\n\ndef second():\n    return 3\n"


def test_shards_partition_changed_lines_exactly_once_and_keep_functions_whole(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text(SHARDED_SOURCE)
    scope = DiffScope("main", {"a.py": frozenset({1, 4, 5, 8})})

    shards = [scope.shard(index, 4) for index in range(4)]

    lines = sorted(line for shard in shards for file in shard.changed_lines.values() for line in file)
    assert lines == [1, 4, 5, 8]
    assert any({4, 5} <= shard.changed_lines.get("a.py", frozenset()) for shard in shards)


def test_a_function_stays_in_its_shard_when_another_file_joins_the_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text(SHARDED_SOURCE)
    (tmp_path / "b.py").write_text("def other():\n    return 4\n")
    alone = DiffScope("main", {"a.py": frozenset({8})})
    joined = DiffScope("main", {"a.py": frozenset({8}), "b.py": frozenset({2})})

    def home(scope: DiffScope) -> int:
        return next(index for index in range(4) if "a.py" in scope.shard(index, 4).changed_lines)

    assert home(alone) == home(joined)


def test_split_by_function_groups_lines_by_innermost_function():
    source = "x = 1\n\ndef outer():\n    def inner():\n        return 1\n    return inner\n"

    assert split_by_function(source, [1, 4, 5, 6]) == [frozenset({1}), frozenset({6}), frozenset({4, 5})]


@pytest.mark.parametrize(("value", "shard"), [(None, None), ("0/4", (0, 4)), ("3/4", (3, 4))])
def test_a_shard_is_an_index_below_its_count(value, shard):
    assert parse_shard(value) == shard


@pytest.mark.parametrize("value", ["4/4", "x/4", "1", "-1/4"])
def test_other_shard_forms_are_rejected(value):
    with pytest.raises(click.BadParameter):
        parse_shard(value)
