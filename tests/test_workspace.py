import subprocess
from pathlib import Path

from mutmut.workspace import MemberRun
from mutmut.workspace import changed_members
from mutmut.workspace import report
from mutmut.workspace import workspace_members


def make_workspace(root: Path, members: str = '["packages/*"]', exclude: str = "[]") -> None:
    (root / "pyproject.toml").write_text(f"[tool.uv.workspace]\nmembers = {members}\nexclude = {exclude}\n")
    for name in ("a", "b", "c"):
        (root / "packages" / name / "src").mkdir(parents=True)
        (root / "packages" / name / "pyproject.toml").write_text("")
    (root / "packages" / "not_a_project").mkdir()


def test_members_are_the_globbed_projects_minus_the_excluded(tmp_path):
    make_workspace(tmp_path, exclude='["packages/c"]')

    assert workspace_members(tmp_path) == [Path("packages/a"), Path("packages/b")]


def test_only_members_with_python_changes_run(tmp_path):
    make_workspace(tmp_path)
    for name in ("a", "b"):
        (tmp_path / "packages" / name / "src" / "x.py").write_text("x = 1\n")
    (tmp_path / "packages" / "c" / "README.md").write_text("docs\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True)
    (tmp_path / "packages" / "b" / "src" / "x.py").write_text("x = 2\n")
    (tmp_path / "packages" / "c" / "README.md").write_text("more docs\n")

    assert changed_members(tmp_path, "HEAD") == [Path("packages/b")]


def test_the_workspace_fails_when_any_member_fails(capsys):
    runs = [MemberRun(Path("packages/a"), 0, "a ok\n"), MemberRun(Path("packages/b"), 1, "b survivors\n")]

    assert report(runs) == 1
    assert report(runs[:1]) == 0
    output = capsys.readouterr().out
    assert "===== packages/b (exit 1) =====\nb survivors" in output
    assert "Mutation testing failed for: packages/b" in output
