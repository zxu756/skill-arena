"""M2 Stage 2 tests for single-point destructive mutation verification (Python AST & Swift line-level)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from skill_arena.cli import main


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test Author",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git_repo(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    for rel_path, content in files.items():
        dest = repo / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_python_ast_mutation_killed_and_restored_clean_worktree(tmp_path: Path) -> None:
    from skill_arena.verifier.env_guard import repo_fingerprint

    files = {
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
        "src/calc.py": (
            "def clamp(val: int, lo: int, hi: int) -> int:\n"
            "    if val < lo:\n"
            "        return lo\n"
            "    if val > hi:\n"
            "        return hi\n"
            "    return val\n\n"
            "def add_offset(a: int, b: int) -> int:\n"
            "    return a + b\n"
        ),
        "tests/test_calc.py": (
            "from calc import add_offset, clamp\n\n"
            "def test_clamp_bounds() -> None:\n"
            "    assert clamp(-5, 0, 10) == 0\n"
            "    assert clamp(5, 0, 10) == 5\n"
            "    assert clamp(25, 0, 10) == 10\n\n"
            "def test_add_offset() -> None:\n"
            "    assert add_offset(3, 4) == 7\n"
        ),
        "README.md": "# Calc\n\n```bash\npython3 -c \"import sys; sys.path.insert(0, 'src'); from calc import clamp; assert clamp(5, 0, 10) == 5\"\n```\n",
    }
    repo = tmp_path / "good_calc_repo"
    _init_git_repo(repo, files)

    fp_before = repo_fingerprint(repo)
    json_out = tmp_path / "good_mut.json"
    md_out = tmp_path / "good_mut.md"
    rc = main(
        [
            "--lang",
            "zh",
            "verify",
            "--repo",
            str(repo),
            "--max-mutations",
            "2",
            "--json-out",
            str(json_out),
            "--md-out",
            str(md_out),
        ]
    )
    fp_after = repo_fingerprint(repo)

    assert rc == 0
    assert fp_before == fp_after
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "PASS"
    mut = data["mutation"]
    assert mut["enabled"] is True
    assert mut["status"] == "PASS"
    assert len(mut["items"]) == 2
    # Deterministic (path, line, col) order
    assert (mut["items"][0]["file"], mut["items"][0]["line_no"]) <= (
        mut["items"][1]["file"],
        mut["items"][1]["line_no"],
    )
    for item in mut["items"]:
        assert item["file"] == "src/calc.py"
        assert item["verdict"] == "KILLED"
        assert item["restored_tests_passed"] is True
        assert item["worktree_clean"] is True
    assert "src/calc.py" in md_out.read_text(encoding="utf-8")


def test_mutation_survived_on_assert_true_repo_fails_verify(tmp_path: Path) -> None:
    files = {
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
        "src/calc.py": (
            "def compute_discount(price: int, rate: int) -> int:\n"
            "    if rate <= 0:\n"
            "        return price\n"
            "    return price - rate\n"
        ),
        "tests/test_calc.py": (
            "import calc  # imported but never meaningfully asserted\n\n"
            "def test_always_passes() -> None:\n"
            "    assert True\n"
        ),
        "README.md": "# Fake Tests\n\n```bash\npython3 -c \"print('ok')\"\n```\n",
    }
    repo = tmp_path / "assert_true_repo"
    _init_git_repo(repo, files)

    json_out = tmp_path / "survived.json"
    md_out = tmp_path / "survived.md"
    rc = main(
        [
            "--lang",
            "zh",
            "verify",
            "--repo",
            str(repo),
            "--json-out",
            str(json_out),
            "--md-out",
            str(md_out),
        ]
    )
    assert rc == 1
    data = json.loads(json_out.read_text(encoding="utf-8"))
    md_text = md_out.read_text(encoding="utf-8")

    assert data["conclusion"] == "FAIL"
    assert data["build_and_test"]["test"]["status"] == "PASS"
    assert data["mutation"]["status"] == "FAIL"
    assert len(data["mutation"]["items"]) == 1
    survived = data["mutation"]["items"][0]
    assert survived["verdict"] == "SURVIVED"
    assert survived["file"] == "src/calc.py"
    assert survived["line_no"] >= 2
    assert survived["worktree_clean"] is True
    assert "src/calc.py" in md_text
    assert str(survived["line_no"]) in md_text


def test_syntax_import_compile_and_collection_errors_do_not_count_as_killed(tmp_path: Path) -> None:
    from skill_arena.verifier.env_guard import build_isolated_verify_env, cleanup_temp_workspace, clone_clean_repo, create_verify_workspace
    from skill_arena.verifier.mutator import (
        MutationCandidate,
        classify_mutation_test_run,
        evaluate_mutation_candidate,
    )

    # 1. Direct classifier rules: syntax, import, collection, and compile errors are INVALID (never KILLED)
    v_syn_exit2, _ = classify_mutation_test_run(
        "pytest",
        2,
        "ERROR collecting tests/test_calc.py\nE     File 'calc.py', line 2\nE       def add(:\nE   SyntaxError: invalid syntax\nInterrupted: 1 error during collection",
    )
    assert v_syn_exit2 == "INVALID"

    v_syn_exit1, _ = classify_mutation_test_run(
        "pytest",
        1,
        "FAILED tests/test_lazy.py::test_import - SyntaxError: invalid syntax (calc.py, line 2)\n1 failed in 0.05s",
    )
    assert v_syn_exit1 == "INVALID"

    v_imp_exit2, _ = classify_mutation_test_run(
        "pytest",
        2,
        "ImportError while importing test module 'tests/test_calc.py'.\nModuleNotFoundError: No module named 'nonexistent_dep'",
    )
    assert v_imp_exit2 == "INVALID"

    v_unit_err, _ = classify_mutation_test_run(
        "python3 -m unittest discover -s tests",
        1,
        "ERROR: test_calc (unittest.loader._FailedTest.test_calc)\nSyntaxError: invalid syntax\nRan 1 test in 0.001s\nFAILED (errors=1)",
    )
    assert v_unit_err == "INVALID"

    v_compile, _ = classify_mutation_test_run(
        "swift test --disable-sandbox",
        1,
        "error: cannot find 'bogus' in scope",
        compile_failed=True,
    )
    assert v_compile == "INVALID"

    v_assert_kill, _ = classify_mutation_test_run(
        "pytest",
        1,
        "FAILED tests/test_calc.py::test_add - AssertionError: assert -1 == 5\n1 failed in 0.04s",
    )
    assert v_assert_kill == "KILLED"

    v_survived, _ = classify_mutation_test_run(
        "pytest",
        0,
        "1 passed in 0.02s",
    )
    assert v_survived == "SURVIVED"

    # 2. Candidate evaluator on an actual syntax-broken mutation in a real clone must return INVALID and restore cleanly
    src_files = {
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
        "src/mod.py": "def target_dep() -> str:\n    return 'math'\n",
        "tests/test_mod.py": (
            "import importlib\n"
            "import mod\n"
            "_LOADED = importlib.import_module(mod.target_dep())\n\n"
            "def test_trivial() -> None:\n"
            "    assert True\n"
        ),
        "README.md": "# Collection Error Repo\n\n```bash\npython3 -c \"print('ok')\"\n```\n",
    }
    repo = tmp_path / "collection_err_repo"
    sha = _init_git_repo(repo, src_files)

    ws = create_verify_workspace()
    try:
        env = build_isolated_verify_env(ws)
        clone_res = clone_clean_repo(repo, sha, ws, env)
        assert clone_res.returncode == 0

        syntax_cand = MutationCandidate(
            language="python",
            rel_path="src/mod.py",
            line_no=1,
            col_offset=0,
            operator="injected_syntax_error",
            description="Inject invalid Python syntax",
            mutated_source="def target_dep( -> str:\n    return 'math'\n",
        )
        rec = evaluate_mutation_candidate(
            ws.repo_dir,
            syntax_cand,
            "pytest",
            env,
            timeout_sec=30,
            lang="zh",
        )
        assert rec.verdict == "INVALID"
        assert rec.worktree_clean is True
        st = subprocess.run(["git", "-C", str(ws.repo_dir), "status", "--porcelain"], capture_output=True, text=True, check=True)
        assert st.stdout == ""
    finally:
        cleanup_temp_workspace(ws.tmp_base)

    # 3. End-to-end verify on `collection_err_repo`: mutating `return 'math'` -> `return ''` causes collection ValueError/ImportError (exit 2), NOT AssertionError -> must FAIL verify (exit 1)!
    j_col = tmp_path / "col_err.json"
    rc_col = main(["--lang", "zh", "verify", "--repo", str(repo), "--json-out", str(j_col)])
    assert rc_col == 1
    d_col = json.loads(j_col.read_text(encoding="utf-8"))
    assert d_col["conclusion"] == "FAIL"
    assert d_col["mutation"]["status"] == "FAIL"


@pytest.mark.skipif(shutil.which("swift") is None, reason="swift toolchain required")
def test_swift_package_line_mutation_killed_and_survived(tmp_path: Path) -> None:
    good_swift = {
        "Package.swift": (
            "// swift-tools-version: 6.0\n"
            "import PackageDescription\n"
            "let package = Package(\n"
            '    name: "TinyMath",\n'
            "    targets: [\n"
            '        .target(name: "TinyMath"),\n'
            '        .testTarget(name: "TinyMathTests", dependencies: ["TinyMath"]),\n'
            "    ]\n"
            ")\n"
        ),
        "Sources/TinyMath/TinyMath.swift": (
            "public func addValues(_ a: Int, _ b: Int) -> Int {\n"
            "    return a + b\n"
            "}\n"
        ),
        "Tests/TinyMathTests/TinyMathTests.swift": (
            "import Testing\n"
            "@testable import TinyMath\n\n"
            "@Test func additionMatches() {\n"
            "    #expect(addValues(2, 3) == 5)\n"
            "}\n"
        ),
        "README.md": "# TinyMath\n\n```bash\necho tiny-math-ok\n```\n",
    }
    good_repo = tmp_path / "swift_good"
    _init_git_repo(good_repo, good_swift)
    j_good = tmp_path / "swift_good.json"
    rc_good = main(["--lang", "en", "verify", "--repo", str(good_repo), "--json-out", str(j_good)])
    assert rc_good == 0
    d_good = json.loads(j_good.read_text(encoding="utf-8"))
    assert d_good["mutation"]["status"] == "PASS"
    assert d_good["mutation"]["items"][0]["verdict"] == "KILLED"
    assert d_good["mutation"]["items"][0]["worktree_clean"] is True

    bad_swift = dict(good_swift)
    bad_swift["Tests/TinyMathTests/TinyMathTests.swift"] = (
        "import Testing\n"
        "@testable import TinyMath\n\n"
        "@Test func weakAssertion() {\n"
        "    #expect(Bool(true))\n"
        "}\n"
    )
    bad_repo = tmp_path / "swift_bad"
    _init_git_repo(bad_repo, bad_swift)
    j_bad = tmp_path / "swift_bad.json"
    rc_bad = main(["--lang", "en", "verify", "--repo", str(bad_repo), "--json-out", str(j_bad)])
    assert rc_bad == 1
    d_bad = json.loads(j_bad.read_text(encoding="utf-8"))
    assert d_bad["mutation"]["status"] == "FAIL"
    assert d_bad["mutation"]["items"][0]["verdict"] == "SURVIVED"
