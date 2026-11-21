"""Unit tests for deterministic test-summary normalization in `skill_arena.verifier.report`."""

from __future__ import annotations

from pathlib import Path

from skill_arena.verifier.builder import summarize_command_output
from skill_arena.verifier.report import PathScrubber, VerifyReport, render_verify_json
from skill_arena.verifier.row3_diagnostics import evaluate_row3_runs


def _sample_pytest_output(summary_banner: str) -> str:
    return (
        "============================= test session starts ==============================\n"
        "platform darwin -- Python 3.12.7, pytest-8.3.3, pluggy-1.5.0\n"
        "collected 1160 items / 2 deselected / 1158 selected\n\n"
        "tests/unit_tests/test_tool_schemas.py .                                  [ 93%]\n"
        "tests/unit_tests/test_trust.py ................                          [ 94%]\n"
        "tests/unit_tests/test_tui.py ........................................... [ 98%]\n"
        ".                                                                        [ 98%]\n"
        "tests/unit_tests/test_web_tools.py ................                      [100%]\n\n"
        f"{summary_banner}\n"
    )


def test_pytest_summary_banner_normalizes_across_short_long_and_hms_durations() -> None:
    scrubber = PathScrubber()

    short_even = _sample_pytest_output(
        "================ 1154 passed, 4 skipped, 2 deselected in 51.77s ================"
    )
    short_asymmetric = _sample_pytest_output(
        "================ 1154 passed, 4 skipped, 2 deselected in 9.87s ================="
    )
    long_hms = _sample_pytest_output(
        "=========== 1154 passed, 4 skipped, 2 deselected in 102.31s (0:01:42) ==========="
    )
    long_hms_asymmetric = _sample_pytest_output(
        "========== 1154 passed, 4 skipped, 2 deselected in 102.31s (0:01:42) ==========="
    )
    ansi_colored = _sample_pytest_output(
        "\x1b[32m================ \x1b[32m\x1b[1m1154 passed\x1b[0m\x1b[32m, "
        "4 skipped, 2 deselected in 9.87s ================\x1b[0m"
    )

    variants = [short_even, short_asymmetric, long_hms, long_hms_asymmetric, ansi_colored]

    # Raw multi-line scrubbing must converge across all duration lengths
    scrubbed_raw = [scrubber.scrub(v) for v in variants]
    assert len(set(scrubbed_raw)) == 1
    assert scrubbed_raw[0].endswith("\n=== 1154 passed, 4 skipped, 2 deselected in <TIME> ===\n")
    # Non-summary headers and dot-progress lines remain untouched
    assert "============================= test session starts ==============================" in scrubbed_raw[0]
    assert "tests/unit_tests/test_tui.py ........................................... [ 98%]" in scrubbed_raw[0]
    assert ".                                                                        [ 98%]" in scrubbed_raw[0]

    # `summarize_command_output` (" | "-joined tail) scrubbing must also converge byte-for-byte
    scrubbed_summaries = [scrubber.scrub(summarize_command_output(v)) for v in variants]
    assert len(set(scrubbed_summaries)) == 1
    assert scrubbed_summaries[0].endswith(
        "tests/unit_tests/test_web_tools.py ................                      [100%] "
        "| === 1154 passed, 4 skipped, 2 deselected in <TIME> ==="
    )

    # Very wide multi-category summary where 80-col pytest drops down to a single `=` vs 6-7 `=`,
    # including when 6 tail lines exceed `max_chars=500` in `summarize_command_output`.
    wide_short = "====== 1 failed, 1154 passed, 4 skipped, 2 deselected, 1 xfailed, 3 warnings in 9.87s ======="
    wide_long = "= 1 failed, 1154 passed, 4 skipped, 2 deselected, 1 xfailed, 3 warnings in 102.31s (0:01:42) ="
    assert scrubber.scrub(wide_short) == scrubber.scrub(wide_long)
    assert (
        scrubber.scrub(wide_short)
        == "=== 1 failed, 1154 passed, 4 skipped, 2 deselected, 1 xfailed, 3 warnings in <TIME> ==="
    )
    over_500_short = _sample_pytest_output(wide_short)
    over_500_long = _sample_pytest_output(wide_long)
    assert scrubber.scrub(summarize_command_output(over_500_short)) == scrubber.scrub(
        summarize_command_output(over_500_long)
    )


def test_pytest_summary_banner_preserves_distinct_counts_and_leaves_failures_untouched() -> None:
    scrubber = PathScrubber()

    out_1154 = _sample_pytest_output(
        "================ 1154 passed, 4 skipped, 2 deselected in 9.87s ================"
    )
    out_1153 = _sample_pytest_output(
        "=========== 1153 passed, 4 skipped, 2 deselected in 102.31s (0:01:42) ==========="
    )
    assert scrubber.scrub(out_1154) != scrubber.scrub(out_1153)
    assert scrubber.scrub(summarize_command_output(out_1154)) != scrubber.scrub(
        summarize_command_output(out_1153)
    )

    failing_short = (
        "tests/unit_tests/test_acp.py::test_banner_literal[====] FAILED           [100%]\n"
        "=================================== FAILURES ===================================\n"
        "_________________________ test_banner_literal[====] __________________________\n"
        "E       AssertionError: expected banner '================' == '==='\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/unit_tests/test_acp.py::test_banner_literal[====] - AssertionError\n"
        "================== 1 failed, 57 passed in 1.23s ==================\n"
    )
    failing_long = failing_short.replace(
        "================== 1 failed, 57 passed in 1.23s ==================",
        "============ 1 failed, 57 passed in 102.31s (0:01:42) ============",
    )

    s_short = scrubber.scrub(failing_short)
    s_long = scrubber.scrub(failing_long)
    assert s_short == s_long
    assert "tests/unit_tests/test_acp.py::test_banner_literal[====] FAILED           [100%]" in s_short
    assert "=================================== FAILURES ===================================" in s_short
    assert "_________________________ test_banner_literal[====] __________________________" in s_short
    assert "E       AssertionError: expected banner '================' == '==='" in s_short
    assert "=========================== short test summary info ============================" in s_short
    assert "FAILED tests/unit_tests/test_acp.py::test_banner_literal[====] - AssertionError" in s_short
    assert s_short.endswith("\n=== 1 failed, 57 passed in <TIME> ===\n")

    # Summary-joined form also preserves failure lines while normalizing only the final summary segment
    assert scrubber.scrub(summarize_command_output(failing_short)) == scrubber.scrub(
        summarize_command_output(failing_long)
    )


def test_unittest_and_zero_test_summaries_normalize_durations_and_keep_counts_distinct() -> None:
    scrubber = PathScrubber()

    ut_fast = (
        "............\n"
        "----------------------------------------------------------------------\n"
        "Ran 12 tests in 0.003s\n\n"
        "OK\n"
    )
    ut_slow = (
        "............\n"
        "----------------------------------------------------------------------\n"
        "Ran 12 tests in 102.345s\n\n"
        "OK\n"
    )
    ut_fewer = (
        "...........\n"
        "----------------------------------------------------------------------\n"
        "Ran 11 tests in 0.003s\n\n"
        "OK\n"
    )
    assert scrubber.scrub(ut_fast) == scrubber.scrub(ut_slow)
    assert scrubber.scrub(summarize_command_output(ut_fast)) == scrubber.scrub(
        summarize_command_output(ut_slow)
    )
    assert "----------------------------------------------------------------------" in scrubber.scrub(ut_fast)
    assert "Ran 12 tests in <TIME>" in scrubber.scrub(ut_fast)
    assert scrubber.scrub(summarize_command_output(ut_fast)) != scrubber.scrub(
        summarize_command_output(ut_fewer)
    )

    zero_fast = "========================= no tests ran in 0.01s =========================="
    zero_slow = "=================== no tests ran in 65.10s (0:01:05) ===================="
    assert scrubber.scrub(zero_fast) == "=== no tests ran in <TIME> ==="
    assert scrubber.scrub(zero_slow) == "=== no tests ran in <TIME> ==="


def test_row3_evaluation_passes_when_two_verify_reports_differ_only_in_raw_pytest_duration_padding(
    tmp_path: Path,
) -> None:
    scrubber = PathScrubber()
    raw_run1 = _sample_pytest_output(
        "================ 1154 passed, 4 skipped, 2 deselected in 9.87s ================"
    )
    raw_run2 = _sample_pytest_output(
        "=========== 1154 passed, 4 skipped, 2 deselected in 102.31s (0:01:42) ==========="
    )

    def _build_report(raw_test_out: str, wall_ms: int) -> VerifyReport:
        summary = scrubber.scrub(summarize_command_output(raw_test_out))
        return VerifyReport(
            repo_name="diy-harness",
            ref="HEAD",
            commit_sha="52207fd803d4",
            conclusion="FAIL",
            exit_code=1,
            env_guard={"source_repo_untouched": True, "temp_copy_deleted": True},
            build_and_test={
                "build": {"name": "build", "status": "PASS", "exit_code": 0, "output_summary": "Checked 35 packages in <TIME>"},
                "test": {"name": "test", "status": "PASS", "exit_code": 0, "output_summary": summary},
            },
            readme_replay={"status": "FAIL", "items": [{"index": 2, "output_summary": summary}]},
            mutation={"status": "PASS", "killed_count": 1},
            self_report={"status": "SKIPPED"},
            failures=["readme: bash scripts/ci_sim.sh (exit 1)"],
            timing_meta={"total_wall_ms": wall_ms},
        )

    j1, m1 = tmp_path / "r1.json", tmp_path / "r1.md"
    j2, m2 = tmp_path / "r2.json", tmp_path / "r2.md"
    j1.write_text(render_verify_json(_build_report(raw_run1, 9870)), encoding="utf-8")
    j2.write_text(render_verify_json(_build_report(raw_run2, 102310)), encoding="utf-8")
    m1.write_text("# r1\n", encoding="utf-8")
    m2.write_text("# r2\n", encoding="utf-8")

    ev_dir = tmp_path / "evidence"
    res = evaluate_row3_runs(
        rc1=1,
        rc2=1,
        run1_json=j1,
        run2_json=j2,
        run1_md=m1,
        run2_md=m2,
        evidence_dir=ev_dir,
    )
    assert res.passed is True
    assert res.canonical_equal is True
    assert res.prefix_equal is True
    assert not ev_dir.exists()
