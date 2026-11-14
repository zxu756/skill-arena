"""Scoped tests for sandbox timeout failure, orphan descendant reaping (K5), and host-HOME consent gate (K2)."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time

import pytest

from skill_arena.models import ContenderConfig
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.suites.builtin_tasks import load_builtin_suite


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _make_stub_cli(bin_dir: Path, name: str, python_body: str) -> Path:
    script_path = bin_dir / name
    script_path.write_text(f"#!/usr/bin/env python3\n{python_body}\n", encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def test_real_cli_requires_explicit_unisolated_home_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran.txt"
    _make_stub_cli(
        bin_dir,
        "claude",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)  # allow_unisolated_host_home defaults to False
    task = load_builtin_suite()[0]
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Stub Claude",
        role="control",
        cli="claude",
        model="stub-model",
    )
    with pytest.raises(PermissionError, match="allow-unisolated-host-home"):
        driver.run_trial("exp_k2", task, contender, rep_index=1)
    assert not marker.exists(), "Agent CLI must never be spawned without explicit consent"
    pool.close()


def test_run_bounded_command_kills_timed_out_leader_and_process_group(tmp_path: Path) -> None:
    import sys

    pid_file = tmp_path / "leader.pid"
    code = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    try:
        rc, _, _, timed_out = WorktreeSandboxPool.run_bounded_command(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            timeout_sec=1,
        )
        assert timed_out is True
        assert rc == 124
        assert pid_file.is_file()
        leader_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert not _pid_is_alive(leader_pid), f"Timed-out leader PID {leader_pid} survived after timeout!"
    finally:
        if pid_file.is_file():
            raw = pid_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                try:
                    os.kill(int(raw), 9)
                except OSError:
                    pass


def test_agent_timeout_fails_even_when_solution_written_and_kills_sleeper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "sleeper.pid"
    task = load_builtin_suite()[0]
    task.timeout_sec = 1
    ref_code = task.reference_solution_files["rate_limiter.py"]

    _make_stub_cli(
        bin_dir,
        "opencode",
        (
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
            "child = subprocess.Popen(['sleep', '30'])\n"
            f"Path({str(pid_file)!r}).write_text(f'{{os.getpid()}},{{child.pid}}', encoding='utf-8')\n"
            "time.sleep(30)\n"
        ),
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, allow_unisolated_host_home=True)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Timeout Stub",
        role="control",
        cli="opencode",
        model="stub-model",
    )
    t0 = time.perf_counter()
    try:
        result = driver.run_trial("exp_timeout", task, contender, rep_index=1)
        elapsed = time.perf_counter() - t0

        assert elapsed < 6.0, f"Timed-out trial took {elapsed:.2f}s instead of aborting promptly"
        assert pid_file.is_file()
        tracked_pids = [int(x) for x in pid_file.read_text(encoding="utf-8").strip().split(",") if x]
        assert len(tracked_pids) == 2
        for pid in tracked_pids:
            assert not _pid_is_alive(pid), f"Timed-out process PID {pid} was not killed!"
        assert not result.passed, "Timed-out trial MUST fail even if passing code was written before timeout"
        assert "timeout" in result.verdict.summary.lower()
    finally:
        pool.close()
        if pid_file.is_file():
            for x in pid_file.read_text(encoding="utf-8").strip().split(","):
                if x.isdigit():
                    try:
                        os.kill(int(x), 9)
                    except OSError:
                        pass


def test_detached_setsid_background_descendant_does_not_hang_and_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids_file = tmp_path / "detached.pids"
    task = load_builtin_suite()[0]
    task.timeout_sec = 8

    # Spawn TWO detached setsid processes:
    # 1) One keeping stdout/stderr pipe open (the exact K5 5m09s hang vector!)
    # 2) One redirecting stdio to DEVNULL while escaping process group
    _make_stub_cli(
        bin_dir,
        "opencode",
        (
            "import subprocess\n"
            "from pathlib import Path\n"
            "p_pipe = subprocess.Popen(['sleep', '120'], start_new_session=True)\n"
            "p_null = subprocess.Popen(\n"
            "    ['sleep', '120'],\n"
            "    start_new_session=True,\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            f"Path({str(pids_file)!r}).write_text(f'{{p_pipe.pid}},{{p_null.pid}}', encoding='utf-8')\n"
            "print('stub exiting immediately')\n"
        ),
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, allow_unisolated_host_home=True)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Detached Spawner Stub",
        role="control",
        cli="opencode",
        model="stub-model",
    )
    t0 = time.perf_counter()
    try:
        result = driver.run_trial("exp_orphan", task, contender, rep_index=1)
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, f"Detached background process stalled trial for {elapsed:.2f}s!"
        raw_pids = [int(x) for x in pids_file.read_text(encoding="utf-8").strip().split(",") if x]
        assert len(raw_pids) == 2
        for pid in raw_pids:
            assert not _pid_is_alive(pid), f"Detached background PID {pid} was not reaped!"
        assert not result.passed
    finally:
        pool.close()
        if pids_file.is_file():
            for x in pids_file.read_text(encoding="utf-8").strip().split(","):
                if x.isdigit():
                    try:
                        os.kill(int(x), 9)
                    except OSError:
                        pass
