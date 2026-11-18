"""Failure categories, complete grader output and the vendored sloplint."""

from __future__ import annotations

from pathlib import Path
import shutil
import stat
import sys

import pytest

from _pool_fixture import CALC_REFERENCE, make_pool
from skill_arena.cli import main
from skill_arena.graders import frontend_craft
from skill_arena.graders.deterministic import GRADER_OUTPUT_CAP, cap_grader_output, evaluate_sandbox
from skill_arena.graders.frontend_craft import VENDORED_SLOPLINT, audit_html_craft, resolve_sloplint_script
import skill_arena.runners.harness_driver as harness_driver_mod
from skill_arena.models import ContenderConfig, GraderVerdict, TaskSpec, TokenUsageLedger, TrialResult
from skill_arena.outcomes import classify_trial_failure, effective_failure_kind
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.templates import render_arena_html
from skill_arena.suites.builtin_tasks import load_builtin_suite
from skill_arena.suites.pool_import import load_pool_task


def _v(**kw) -> GraderVerdict:
    base = {"passed": False, "exit_code": 1, "anti_cheat_clean": True, "summary": "Tests exited with code 1"}
    base.update(kw)
    return GraderVerdict(**base)


@pytest.mark.parametrize(
    ("verdict", "kwargs", "expected"),
    [
        (_v(passed=True, exit_code=0, summary="Unit tests passed (exit 0)"), {}, "passed"),
        (_v(), {}, "wrong"),
        (_v(exit_code=124, summary="AGENT TIMEOUT: agent execution exceeded 900s"), {"interrupted": True}, "interrupted"),
        (_v(), {"relay_status": "failed (exit 3: outcome deadline, stop_reason deadline)"}, "wrong"),
        (_v(anti_cheat_clean=False, tampered_test_files=["tests/t.py"]), {}, "anti_cheat"),
        (_v(leaked_processes=[{"pid": 1, "reaped": True}]), {}, "anti_cheat"),
        (_v(exit_code=0, sloplint_fails=2, summary="Unit tests passed (exit 0); Craft gate 2 FAIL / 0 WARN"), {}, "craft"),
        (_v(exit_code=5, sloplint_fails=2), {}, "wrong"),
        (_v(), {"relay_wrote_log": False}, "infra"),
        (
            _v(),
            {"relay_status": "failed (exit 1: looks like a quota or rate limit: don't retry, tell the user)"},
            "infra",
        ),
        (_v(), {"relay_status": "failed (exit 1)", "agent_texts": ("HTTP 429 Too Many Requests",)}, "infra"),
        # A successful agent talking about rate limits (the token-bucket task) is not an infra failure.
        (_v(), {"relay_status": "ok", "agent_texts": ("Implemented the rate limiter quota",)}, "wrong"),
        (_v(), {"relay_status": "ok", "agent_produced_output": False}, "infra"),
        (_v(summary="GRADER ERROR: OSError: disk full"), {}, "grader_error"),
    ],
)
def test_classify_trial_failure(verdict: GraderVerdict, kwargs: dict, expected: str) -> None:
    kwargs = {"interrupted": False, **kwargs}
    assert classify_trial_failure(verdict, **kwargs) == expected


def test_relay_status_deadline_counts_as_interrupted_when_flag_set() -> None:
    v = _v()
    assert classify_trial_failure(v, interrupted=True, relay_status="failed (exit 3: outcome deadline)") == "interrupted"


def _trial(kind: str, passed: bool = False, **verdict_kw) -> TrialResult:
    return TrialResult(
        trial_id=f"tr_{kind}_{passed}",
        experiment_id="exp",
        task_id="t1",
        task_title="T",
        task_category="parse",
        contender_id="arm_control",
        contender_label="ctrl",
        contender_role="control",
        rep_index=1,
        passed=passed,
        anti_cheat_clean=True,
        usage=TokenUsageLedger(),
        verdict=_v(passed=passed, failure_kind=kind, **verdict_kw),
        steps=[],
        git_diff="",
        started_at="",
        finished_at="",
    )


def test_effective_failure_kind_derives_legacy_rows() -> None:
    assert effective_failure_kind(_trial("infra")) == "infra"
    assert effective_failure_kind(_trial("", passed=True, exit_code=0)) == "passed"
    assert effective_failure_kind(_trial("", exit_code=124, summary="AGENT TIMEOUT: x")) == "interrupted"
    assert effective_failure_kind(_trial("")) == "wrong"


def test_grader_crash_is_recorded_as_grader_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise OSError("grading disk vanished")

    monkeypatch.setattr(harness_driver_mod, "evaluate_sandbox", _boom)
    task = load_builtin_suite(category="concurrency")[0]
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    ctrl = ContenderConfig(contender_id="arm_control", label="sim", role="control", cli="simulate:baseline", model="sim")
    try:
        result = driver.run_trial("exp", task, ctrl, rep_index=1)
    finally:
        pool.close()
    assert result.passed is False
    assert result.verdict.failure_kind == "grader_error"
    assert "grading disk vanished" in result.verdict.summary
    assert "Traceback" in result.verdict.stdout_tail


def _stub(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_with_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> TrialResult:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(_stub(bin_dir, "claude", body)))
    task = load_pool_task(make_pool(tmp_path / "taskpool") / "calc-01-basics", "hidden")
    task.timeout_sec = 60
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, upstream_api_url="http://127.0.0.1:9")
    contender = ContenderConfig(contender_id="arm_control", label="c", role="control", cli="claude", model="m")
    try:
        return driver.run_trial("exp", task, contender, rep_index=1)
    finally:
        pool.close()


def test_quota_error_from_cli_is_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_with_stub(
        tmp_path,
        monkeypatch,
        "import sys\nsys.stdin.read()\nprint('API Error: 429 rate_limit_error: usage limit reached', file=sys.stderr)\nsys.exit(1)",
    )
    assert not result.passed
    assert result.verdict.failure_kind == "infra", result.usage.relay_status
    assert result.agent_wall_ms is not None and result.agent_wall_ms >= 0


def test_agent_that_answers_but_is_wrong_is_wrong_not_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "import json, sys\nfrom pathlib import Path\nsys.stdin.read()\n"
        f"Path('calc.py').write_text({CALC_REFERENCE.replace('a + b', 'a - b')!r})\n"
        "print(json.dumps({'result': 'Done.\\nSTATUS: success', 'total_cost_usd': 0.001}))"
    )
    result = _run_with_stub(tmp_path, monkeypatch, body)
    assert result.verdict.failure_kind == "wrong"
    # Complete grader output: the unittest failure report, not just a tail.
    assert "FAIL: test_add" in result.verdict.stdout_tail
    assert "Ran 3 tests" in result.verdict.stdout_tail


def test_cap_grader_output_keeps_head_and_tail() -> None:
    short = "x" * 5000
    assert cap_grader_output(short) == short
    big = "HEAD" + "y" * (GRADER_OUTPUT_CAP * 2) + "TAIL"
    capped = cap_grader_output(big)
    assert capped.startswith("HEAD") and capped.endswith("TAIL")
    assert len(capped) < GRADER_OUTPUT_CAP + 200
    assert GRADER_OUTPUT_CAP >= 200_000


def test_long_test_output_is_stored_whole(tmp_path: Path) -> None:
    test_src = (
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_noisy(self):\n"
        "        for i in range(3000):\n            print(f'line-{i:05d}')\n        self.fail('boom')\n"
    )
    task = TaskSpec(
        task_id="noisy",
        title="Noisy",
        category="parse",
        prompt="x",
        test_command="python3 -m unittest tests/test_noisy.py",
        protected_test_paths=["tests/test_noisy.py"],
        seed_files={"tests/test_noisy.py": test_src, "m.py": "X = 1\n"},
    )
    pool = WorktreeSandboxPool(tmp_path / "pool")
    handle = pool.acquire(task, "noisy")
    try:
        verdict, _ = evaluate_sandbox(handle, task)
    finally:
        pool.release(handle)
        pool.close()
    assert "line-00000" in verdict.stdout_tail and "line-02999" in verdict.stdout_tail
    assert len(verdict.stdout_tail) > 30_000


def test_vendored_sloplint_is_default_and_licensed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILL_ARENA_SLOPLINT", raising=False)
    script, origin = resolve_sloplint_script()
    assert script == VENDORED_SLOPLINT and script.is_file()
    assert "vendored hallmark v2.0.0" in origin
    vendor_dir = VENDORED_SLOPLINT.parent
    assert "MIT License" in (vendor_dir / "LICENSE").read_text(encoding="utf-8")
    notice = (vendor_dir / "NOTICE.txt").read_text(encoding="utf-8")
    assert "Nutlope/hallmark" in notice and "2.0.0" in notice
    # sloplint.mjs imports sloplint-render.mjs; both are vendored.
    assert "./sloplint-render.mjs" in VENDORED_SLOPLINT.read_text(encoding="utf-8")
    assert (vendor_dir / "sloplint-render.mjs").is_file()
    assert ".agents/skills/hallmark" not in Path(frontend_craft.__file__).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_sloplint_env_override_is_used_and_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake-sloplint.mjs"
    fake.write_text(
        "console.log('sloplint: 2 FAIL, 1 REFLEX, 4 WARN');\nconsole.log('FAIL 12 page.html: first finding');\n"
        "console.log('WARN 30 page.html: last finding');\n",
        encoding="utf-8",
    )
    page = tmp_path / "page.html"
    page.write_text(
        '<!doctype html><html><head><meta name="description" content="x"><style>h1{text-wrap: balance}</style>'
        "</head><body><h1>Hi</h1></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKILL_ARENA_SLOPLINT", str(fake))
    audit = audit_html_craft(page)
    assert audit.fails == 3 and audit.warns == 4
    assert "env SKILL_ARENA_SLOPLINT" in audit.sloplint_source and str(fake) in audit.sloplint_source
    assert any("first finding" in f for f in audit.findings) and any("last finding" in f for f in audit.findings)

    monkeypatch.delenv("SKILL_ARENA_SLOPLINT")
    assert "vendored hallmark v2.0.0" in audit_html_craft(page).sloplint_source


def test_frontend_verdict_records_sloplint_and_craft_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILL_ARENA_SLOPLINT", raising=False)
    task = next(t for t in load_builtin_suite() if t.frontend_target_html)
    pool = WorktreeSandboxPool(tmp_path / "pool")
    handle = pool.acquire(task, "ui")
    try:
        verdict, _ = evaluate_sandbox(handle, task)
    finally:
        pool.release(handle)
        pool.close()
    assert verdict.sloplint_source
    assert verdict.craft_findings
    assert all(f in verdict.stdout_tail for f in verdict.craft_findings)


def test_failure_counts_in_report_cli_and_html(tmp_path: Path, capsys) -> None:
    db = ArenaDatabase(tmp_path / "a.sqlite")
    ctrl = ContenderConfig(contender_id="arm_control", label="ctrl", role="control", cli="claude", model="m")
    treat = ContenderConfig(contender_id="arm_treatment", label="treat", role="treatment", cli="claude", model="m")
    db.create_experiment("exp", "T", "s", 1, [ctrl, treat])
    kinds = ["passed", "wrong", "infra", "interrupted", "craft"]
    for i, kind in enumerate(kinds):
        for c in (ctrl, treat):
            t = _trial(kind, passed=kind == "passed")
            t.trial_id = f"tr_{c.contender_id}_{i}"
            t.task_id = f"t{i}"
            t.contender_id, t.contender_label, t.contender_role = c.contender_id, c.label, c.role
            db.record_trial(t)
    report = db.build_comparison_report("exp")
    counts = {r["contender_id"]: r["failure_counts"] for r in report["leaderboard"]}
    assert counts["arm_control"]["infra"] == 1 and counts["arm_control"]["passed"] == 1
    assert counts["arm_treatment"]["craft"] == 1 and counts["arm_treatment"]["anti_cheat"] == 0
    assert all(tr["failure_kind"] for tr in report["trials"])

    assert main(["--lang", "en", "compare", "--db", str(tmp_path / "a.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "Failure categories" in out and "infra 1" in out
    assert main(["--lang", "zh", "compare", "--db", str(tmp_path / "a.sqlite")]) == 0
    assert "环境故障 1" in capsys.readouterr().out

    page = render_arena_html(report, lang="en")
    assert 'id="design-analysis"' in page and "Infra (CLI, quota, no output)" in page
    out_html = tmp_path / "r.html"
    out_html.write_text(page, encoding="utf-8")
    assert audit_html_craft(out_html).passed, audit_html_craft(out_html).findings
    zh_html = tmp_path / "r_zh.html"
    zh_html.write_text(render_arena_html(report, lang="zh"), encoding="utf-8")
    assert "环境故障（CLI、额度、无输出）" in zh_html.read_text(encoding="utf-8")
    assert audit_html_craft(zh_html).passed, audit_html_craft(zh_html).findings
