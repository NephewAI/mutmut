import shutil
import subprocess
import sys
from pathlib import Path

from tests.e2e.e2e_utils import E2E_PROJECTS

SOURCE = Path("src/my_lib/__init__.py")
MUTMUT_CONFIG = "[tool.mutmut]\nstats_from_coverage = true\nmutate_only_covered_lines = true\n"


def committed_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    for name in ("a", "b"):
        member = root / "packages" / name
        shutil.copytree(E2E_PROJECTS / "my_lib", member, ignore=shutil.ignore_patterns("mutants"))
        pyproject = member / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace("[tool.mutmut]\n", MUTMUT_CONFIG))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run([*git, *args], cwd=root, check=True)
    return root


def run_workspace(root: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "mutmut", "run", "--workspace", "--diff-base", "HEAD", "--max-children", "2"]
    return subprocess.run(command, cwd=root, capture_output=True, text=True)


def test_one_call_runs_each_changed_member_and_fails_on_any_survivor(tmp_path):
    root = committed_workspace(tmp_path)
    member_a = root / "packages" / "a"
    (member_a / SOURCE).write_text((member_a / SOURCE).read_text() + "\n\ndef doubled(x):\n    return x * 2\n")
    tests = member_a / "tests" / "test_my_lib.py"
    tests.write_text(tests.read_text() + "\n\ndef test_doubled():\n    assert doubled(3) == 6\n")

    passing = run_workspace(root)

    assert passing.returncode == 0, passing.stdout + passing.stderr
    assert "===== packages/a (exit 0) =====" in passing.stdout
    assert "packages/b" not in passing.stdout

    member_b = root / "packages" / "b"
    source = member_b / SOURCE
    source.write_text(source.read_text().replace('"Mutants for this method should survive"', '"These should survive"'))

    failing = run_workspace(root)

    assert failing.returncode == 1, failing.stdout + failing.stderr
    assert "Mutation testing failed for: packages/b" in failing.stdout
    assert "===== packages/a (exit 0) =====" in failing.stdout
