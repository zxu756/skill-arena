"""Project build/test command auto-detection and zero-tests-ran enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from skill_arena.verifier.env_guard import CommandRunResult, run_verified_command

_UNITTEST_RAN_RE = re.compile(r"\bRan\s+(\d+)\s+tests?\s+in\b")
_PYTEST_ZERO_RE = re.compile(r"\b(?:no tests ran|collected\s+0\s+items)\b", re.IGNORECASE)
_PYTEST_PASSED_RE = re.compile(r"\b(\d+)\s+passed\b")
_SWIFT_ZERO_RE = re.compile(
    r"(?:\bExecuted\s+0\s+tests\b|\bTest\s+run\s+with\s+0\s+tests\b)",
    re.IGNORECASE,
)
_SWIFT_RAN_RE = re.compile(
    r"(?:\bExecuted\s+(\d+)\s+tests?\b|\bTest\s+run\s+with\s+(\d+)\s+tests?\b)",
    re.IGNORECASE,
)
_CARGO_RUNNING_RE = re.compile(r"\brunning\s+(\d+)\s+tests?\b")

_PYTEST_IMPORT_RE = re.compile(r"^\s*(?:import\s+pytest|from\s+pytest\s+import)\b", re.MULTILINE)
_UNITTEST_CASE_RE = re.compile(r"\bunittest\.TestCase\b")
_BARE_TEST_DEF_RE = re.compile(r"^def\s+test_[A-Za-z0-9_]*\s*\(", re.MULTILINE)


@dataclass(slots=True)
class StepExecutionRecord:
    """Structured outcome of the build step or baseline test step."""

    name: str
    command: str | None
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    exit_code: int | None
    zero_tests_ran: bool = False
    timed_out: bool = False
    leaked_process_count: int = 0
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)
    reaped_pids: list[int] = field(default_factory=list)
    reason: str = ""
    output_summary: str = ""
    wall_ms: int = 0


def _collect_python_test_files(repo_dir: Path) -> list[Path]:
    found: list[Path] = []
    for rel_dir in ("tests", "test"):
        sub = repo_dir / rel_dir
        if sub.is_dir():
            for p in sorted(sub.rglob("*.py")):
                if p.name.startswith("test_") or p.name.endswith("_test.py"):
                    found.append(p)
    for p in sorted(repo_dir.glob("test_*.py")):
        found.append(p)
    for p in sorted(repo_dir.glob("*_test.py")):
        if p not in found:
            found.append(p)
    return found


def _prefers_pytest(repo_dir: Path, py_test_files: list[Path]) -> bool:
    for cfg in ("pytest.ini", ".pytest.ini", "conftest.py", "tests/conftest.py"):
        if (repo_dir / cfg).is_file():
            return True
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        txt = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in txt or "pytest" in txt:
            return True
    setup_cfg = repo_dir / "setup.cfg"
    if setup_cfg.is_file():
        txt = setup_cfg.read_text(encoding="utf-8", errors="replace")
        if "[tool:pytest]" in txt:
            return True
    uv_lock = repo_dir / "uv.lock"
    if uv_lock.is_file():
        txt = uv_lock.read_text(encoding="utf-8", errors="replace")
        if 'name = "pytest"' in txt:
            return True

    uses_pytest = False
    uses_unittest = False
    has_bare_defs = False
    for path in py_test_files[:40]:
        src = path.read_text(encoding="utf-8", errors="replace")
        if _PYTEST_IMPORT_RE.search(src):
            uses_pytest = True
        if _UNITTEST_CASE_RE.search(src):
            uses_unittest = True
        if _BARE_TEST_DEF_RE.search(src):
            has_bare_defs = True

    if uses_pytest or (has_bare_defs and not uses_unittest):
        return True
    return False


def _unittest_discover_cmd(repo_dir: Path, runner_prefix: str = "python3") -> str:
    if (repo_dir / "tests").is_dir() and not any(repo_dir.glob("test_*.py")):
        if (repo_dir / "tests" / "__init__.py").is_file():
            return f"{runner_prefix} -m unittest discover -s tests -t ."
        return f"{runner_prefix} -m unittest discover -s tests"
    if (repo_dir / "test").is_dir() and not any(repo_dir.glob("test_*.py")):
        if (repo_dir / "test" / "__init__.py").is_file():
            return f"{runner_prefix} -m unittest discover -s test -t ."
        return f"{runner_prefix} -m unittest discover -s test"
    return f"{runner_prefix} -m unittest discover"


def detect_build_and_test_commands(repo_dir: Path) -> tuple[str | None, str | None]:
    """Deterministically infer `(build_cmd, test_cmd)` from project configuration files."""
    py_tests = _collect_python_test_files(repo_dir)

    if (repo_dir / "uv.lock").is_file():
        build_cmd = "uv sync --offline --frozen"
        test_cmd = (
            "uv run pytest"
            if _prefers_pytest(repo_dir, py_tests)
            else _unittest_discover_cmd(repo_dir, "uv run python")
        )
        return build_cmd, test_cmd

    if (repo_dir / "Package.swift").is_file():
        return "swift build --disable-sandbox", "swift test --disable-sandbox"

    if (repo_dir / "Cargo.toml").is_file():
        return "cargo build --offline", "cargo test --offline"

    if (repo_dir / "package.json").is_file() and not py_tests and not (repo_dir / "pyproject.toml").is_file():
        return None, "npm test"

    has_py_markers = (
        bool(py_tests)
        or any(
            (repo_dir / marker).exists()
            for marker in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", ".pytest.ini", "tests", "test")
        )
        or any(repo_dir.glob("*.py"))
    )
    if has_py_markers:
        build_cmd = "python3 -m compileall -q ."
        test_cmd = (
            "pytest"
            if _prefers_pytest(repo_dir, py_tests)
            else _unittest_discover_cmd(repo_dir, "python3")
        )
        return build_cmd, test_cmd

    return None, None


def detect_zero_tests_ran(test_command: str, exit_code: int, combined_out: str) -> bool:
    """Return True if the test runner executed or collected 0 tests (preventing false-green passes)."""
    m_unit = _UNITTEST_RAN_RE.search(combined_out)
    if m_unit is not None and int(m_unit.group(1)) == 0:
        return True
    if _PYTEST_ZERO_RE.search(combined_out):
        return True

    m_swift_all = _SWIFT_RAN_RE.findall(combined_out)
    if m_swift_all:
        if sum(int(a or b) for a, b in m_swift_all) == 0:
            return True
    elif _SWIFT_ZERO_RE.search(combined_out):
        return True

    cmd_lower = test_command.lower()
    if "pytest" in cmd_lower and exit_code == 5:
        return True

    if exit_code == 0:
        if "unittest" in cmd_lower and m_unit is None:
            return True
        if "pytest" in cmd_lower:
            m_pass = _PYTEST_PASSED_RE.search(combined_out)
            if m_pass is None or int(m_pass.group(1)) == 0:
                return True
        if "swift test" in cmd_lower:
            m_swift = _SWIFT_RAN_RE.findall(combined_out)
            if m_swift:
                total = sum(int(a or b) for a, b in m_swift)
                if total == 0:
                    return True
        if "cargo test" in cmd_lower:
            counts = [int(x) for x in _CARGO_RUNNING_RE.findall(combined_out)]
            if counts and sum(counts) == 0:
                return True

    return False


def summarize_command_output(output: str, max_lines: int = 6, max_chars: int = 500) -> str:
    """Extract a concise, deterministic tail summary from command output."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return ""
    selected = lines[-max_lines:]
    joined = " | ".join(selected)
    if len(joined) > max_chars:
        return joined[-max_chars:]
    return joined


def execute_build_and_test(
    repo_dir: Path,
    env: dict[str, str],
    *,
    build_cmd_override: str | None = None,
    test_cmd_override: str | None = None,
    timeout_sec: int = 300,
    lang: str = "zh",
) -> tuple[StepExecutionRecord, StepExecutionRecord]:
    """Run build (if applicable) and baseline tests inside `repo_dir`, enforcing >0 tests ran."""
    is_zh = lang == "zh"
    auto_build, auto_test = detect_build_and_test_commands(repo_dir)

    if build_cmd_override is not None:
        eff_build = build_cmd_override.strip() or None
    else:
        eff_build = auto_build

    if test_cmd_override is not None:
        eff_test = test_cmd_override.strip() or None
    else:
        eff_test = auto_test

    # If src/ layout exists without .venv, include repo root and src/ on PYTHONPATH for test step only
    run_env = dict(env)
    if not (repo_dir / ".venv").exists():
        py_paths = [str(repo_dir)]
        if (repo_dir / "src").is_dir():
            py_paths.insert(0, str(repo_dir / "src"))
        run_env["PYTHONPATH"] = ":".join(py_paths)

    if eff_build is None:
        build_rec = StepExecutionRecord(
            name="build",
            command=None,
            status="SKIPPED",
            exit_code=None,
            reason="无需独立构建步骤" if is_zh else "No separate build command required",
        )
    else:
        b_res: CommandRunResult = run_verified_command(
            ["/bin/bash", "-c", eff_build],
            cwd=repo_dir,
            env=run_env,
            timeout_sec=timeout_sec,
        )
        # Once `uv sync` creates `.venv`, drop fallback PYTHONPATH so subsequent steps run cleanly in `.venv`
        if (repo_dir / ".venv").exists():
            run_env.pop("PYTHONPATH", None)
        leaked_n = len(b_res.leaked_processes)
        if b_res.timed_out:
            b_status = "FAIL"
            b_reason = f"构建命令超时（>{timeout_sec}s）" if is_zh else f"Build command timed out (>{timeout_sec}s)"
        elif b_res.returncode != 0:
            b_status = "FAIL"
            b_reason = (
                f"构建命令返回非零退出码 {b_res.returncode}"
                if is_zh
                else f"Build command exited with code {b_res.returncode}"
            )
        elif leaked_n > 0:
            b_status = "FAIL"
            b_reason = (
                f"构建命令遗留 {leaked_n} 个后台进程（已强杀回收）"
                if is_zh
                else f"Build command leaked {leaked_n} background process(es) (reaped)"
            )
        else:
            b_status = "PASS"
            b_reason = "构建成功" if is_zh else "Build succeeded"

        build_rec = StepExecutionRecord(
            name="build",
            command=eff_build,
            status=b_status,
            exit_code=b_res.returncode,
            timed_out=b_res.timed_out,
            leaked_process_count=leaked_n,
            leaked_processes=b_res.leaked_processes,
            reaped_pids=b_res.reaped_pids,
            reason=b_reason,
            output_summary=summarize_command_output(b_res.combined_output),
            wall_ms=b_res.wall_ms,
        )

    if build_rec.status == "FAIL":
        test_rec = StepExecutionRecord(
            name="test",
            command=eff_test,
            status="FAIL",
            exit_code=None,
            reason="因构建步骤失败，基线测试未通过" if is_zh else "Baseline tests blocked by build failure",
        )
        return build_rec, test_rec

    if eff_test is None:
        test_rec = StepExecutionRecord(
            name="test",
            command=None,
            status="FAIL",
            exit_code=None,
            zero_tests_ran=True,
            reason=(
                "无法自动识别测试命令（请通过 --test-cmd 指定）"
                if is_zh
                else "Cannot auto-detect test command (pass --test-cmd explicitly)"
            ),
        )
        return build_rec, test_rec

    t_res = run_verified_command(
        ["/bin/bash", "-c", eff_test],
        cwd=repo_dir,
        env=run_env,
        timeout_sec=timeout_sec,
    )
    combined = t_res.combined_output
    zero_ran = detect_zero_tests_ran(eff_test, t_res.returncode, combined)
    t_leaked = len(t_res.leaked_processes)

    if t_res.timed_out:
        t_status = "FAIL"
        t_reason = f"测试命令超时（>{timeout_sec}s）" if is_zh else f"Test command timed out (>{timeout_sec}s)"
    elif zero_ran:
        t_status = "FAIL"
        t_reason = "测试跑了 0 条（判失败）" if is_zh else "0 tests ran (treated as failure)"
    elif t_res.returncode != 0:
        t_status = "FAIL"
        t_reason = (
            f"测试命令失败（退出码 {t_res.returncode}）"
            if is_zh
            else f"Test command failed (exit code {t_res.returncode})"
        )
    elif t_leaked > 0:
        t_status = "FAIL"
        t_reason = (
            f"测试命令遗留 {t_leaked} 个后台进程（已强杀回收）"
            if is_zh
            else f"Test command leaked {t_leaked} background process(es) (reaped)"
        )
    else:
        t_status = "PASS"
        t_reason = "测试通过且用例数 > 0" if is_zh else "Tests passed (>0 tests executed)"

    test_rec = StepExecutionRecord(
        name="test",
        command=eff_test,
        status=t_status,
        exit_code=t_res.returncode,
        zero_tests_ran=zero_ran,
        timed_out=t_res.timed_out,
        leaked_process_count=t_leaked,
        leaked_processes=t_res.leaked_processes,
        reaped_pids=t_res.reaped_pids,
        reason=t_reason,
        output_summary=summarize_command_output(combined),
        wall_ms=t_res.wall_ms,
    )
    return build_rec, test_rec
