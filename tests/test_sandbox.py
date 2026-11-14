"""Scoped tests for sandbox timeout failure, orphan descendant reaping (K5), and host-HOME consent gate (K2)."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import threading
import time

import pytest

from skill_arena.models import ContenderConfig, TrialResult
from skill_arena.runners.harness_driver import HarnessDriver
from skill_arena.sandbox.worktree_pool import WorktreeSandboxPool
from skill_arena.suites.builtin_tasks import load_builtin_suite


class _PidFileGatedTime:
    """Test clock proxy that pauses `worktree_pool.time.monotonic()` until `pid_file` appears on disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base = time.monotonic()
        self._released_mono: float | None = None

    def release_countdown(self) -> None:
        with self._lock:
            if self._released_mono is None:
                self._released_mono = time.monotonic()

    def monotonic(self) -> float:
        now = time.monotonic()
        with self._lock:
            if self._released_mono is None:
                return self._base
            return self._base + (now - self._released_mono)

    def sleep(self, secs: float) -> None:
        time.sleep(secs)

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


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
    script_path.write_text(f"#!{sys.executable}\n{python_body}\n", encoding="utf-8")
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


def test_run_bounded_command_kills_timed_out_leader_and_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "leader.pid"
    pid_tmp = tmp_path / "leader.pid.tmp"
    # Simulate 1.2s startup delay exceeding timeout_sec=1 to prove countdown starts only after pid_file appears
    code = (
        "import os, time\n"
        "from pathlib import Path\n"
        "time.sleep(1.2)\n"
        f"Path({str(pid_tmp)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"os.replace({str(pid_tmp)!r}, {str(pid_file)!r})\n"
        "time.sleep(30)\n"
    )
    gated_time = _PidFileGatedTime()
    monkeypatch.setattr("skill_arena.sandbox.worktree_pool.time", gated_time)

    box: dict[str, object] = {}

    def _runner() -> None:
        try:
            box["out"] = WorktreeSandboxPool.run_bounded_command(
                [sys.executable, "-c", code],
                cwd=tmp_path,
                timeout_sec=1,
            )
        except BaseException as exc:  # pragma: no cover - surfaced in main thread
            box["err"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    try:
        worker.start()
        startup_deadline = time.perf_counter() + 15.0
        while not pid_file.is_file() and worker.is_alive() and time.perf_counter() < startup_deadline:
            time.sleep(0.01)

        assert pid_file.is_file(), "Child leader did not write pid_file before exiting"
        gated_time.release_countdown()
        t0 = time.perf_counter()
        worker.join(timeout=10.0)
        elapsed = time.perf_counter() - t0

        assert not worker.is_alive(), "run_bounded_command hung past post-pid_file join window"
        if "err" in box:
            raise box["err"]  # type: ignore[misc]
        rc, _, _, timed_out = box["out"]  # type: ignore[misc]
        assert elapsed < 6.0, f"Timed-out command took {elapsed:.2f}s after pid_file appeared (expected < 6.0s)"
        assert timed_out is True
        assert rc == 124
        leader_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert not _pid_is_alive(leader_pid), f"Timed-out leader PID {leader_pid} survived after timeout!"
    finally:
        gated_time.release_countdown()
        if worker.is_alive():
            worker.join(timeout=2.0)
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
    pid_tmp = tmp_path / "sleeper.pid.tmp"
    task = load_builtin_suite()[0]
    task.timeout_sec = 2
    ref_code = task.reference_solution_files["rate_limiter.py"]

    _make_stub_cli(
        bin_dir,
        "opencode",
        (
            "import os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"Path({str(pid_tmp)!r}).write_text(f'{{os.getpid()}},{{child.pid}}', encoding='utf-8')\n"
            f"os.replace({str(pid_tmp)!r}, {str(pid_file)!r})\n"
            "time.sleep(30)\n"
        ),
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    gated_time = _PidFileGatedTime()
    monkeypatch.setattr("skill_arena.sandbox.worktree_pool.time", gated_time)

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, allow_unisolated_host_home=True)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Timeout Stub",
        role="control",
        cli="opencode",
        model="stub-model",
    )
    trial_box: dict[str, object] = {}

    def _run_trial_worker() -> None:
        try:
            trial_box["result"] = driver.run_trial("exp_timeout", task, contender, rep_index=1)
        except BaseException as exc:  # pragma: no cover - surfaced in main thread
            trial_box["err"] = exc

    worker = threading.Thread(target=_run_trial_worker, daemon=True)
    try:
        worker.start()
        startup_deadline = time.perf_counter() + 15.0
        while not pid_file.is_file() and worker.is_alive() and time.perf_counter() < startup_deadline:
            time.sleep(0.01)

        assert pid_file.is_file(), "Stub agent did not write sleeper.pid before exiting"
        gated_time.release_countdown()
        t0 = time.perf_counter()
        worker.join(timeout=15.0)
        elapsed = time.perf_counter() - t0

        assert not worker.is_alive(), "run_trial hung past post-pid_file join window"
        if "err" in trial_box:
            raise trial_box["err"]  # type: ignore[misc]
        result = trial_box["result"]
        assert isinstance(result, TrialResult)

        assert elapsed < 6.0, f"Timed-out trial took {elapsed:.2f}s after pid_file appeared instead of < 6.0s"
        tracked_pids = [int(x) for x in pid_file.read_text(encoding="utf-8").strip().split(",") if x]
        assert len(tracked_pids) == 2
        for pid in tracked_pids:
            assert not _pid_is_alive(pid), f"Timed-out process PID {pid} was not killed!"
        assert not result.passed, "Timed-out trial MUST fail even if passing code was written before timeout"
        assert "agent timeout" in result.verdict.summary.lower()
    finally:
        gated_time.release_countdown()
        if worker.is_alive():
            worker.join(timeout=2.0)
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


def test_detached_double_fork_non_sip_daemon_is_killed_and_fails_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K13: Stub writes passing answer, then double-forks + setsid + closes fds 3..255 + chdir('/') + sleeps."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    daemon_pid_file = tmp_path / "daemon.pid"
    daemon_pid_tmp = tmp_path / "daemon.pid.tmp"
    task = load_builtin_suite()[0]
    task.timeout_sec = 8
    ref_code = task.reference_solution_files["rate_limiter.py"]

    _make_stub_cli(
        bin_dir,
        "opencode",
        (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
            "pid1 = os.fork()\n"
            "if pid1 == 0:\n"
            "    os.setsid()\n"
            "    pid2 = os.fork()\n"
            "    if pid2 == 0:\n"
            "        os.chdir('/')\n"
            "        for fd in range(3, 256):\n"
            "            try:\n"
            "                os.close(fd)\n"
            "            except OSError:\n"
            "                pass\n"
            "        dn = os.open(os.devnull, os.O_RDWR)\n"
            "        os.dup2(dn, 0)\n"
            "        os.dup2(dn, 1)\n"
            "        os.dup2(dn, 2)\n"
            "        if dn > 2:\n"
            "            os.close(dn)\n"
            f"        Path({str(daemon_pid_tmp)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            f"        os.replace({str(daemon_pid_tmp)!r}, {str(daemon_pid_file)!r})\n"
            "        os.execv(sys.executable, [sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "    os._exit(0)\n"
            "os.waitpid(pid1, 0)\n"
            "for _ in range(50):\n"
            f"    if Path({str(daemon_pid_file)!r}).is_file():\n"
            "        break\n"
            "    time.sleep(0.01)\n"
        ),
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool, allow_unisolated_host_home=True)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Double-Fork Daemon Stub",
        role="control",
        cli="opencode",
        model="stub-model",
    )
    try:
        result = driver.run_trial("exp_k13", task, contender, rep_index=1)
        assert daemon_pid_file.is_file(), "Detached daemon did not record its PID"
        daemon_pid = int(daemon_pid_file.read_text(encoding="utf-8").strip())

        assert not _pid_is_alive(daemon_pid), f"Escaped non-SIP daemon PID {daemon_pid} survived trial cleanup!"
        assert not result.passed, "Trial leaving escaped background daemon MUST be marked FAILED even with passing fix!"
        assert result.leaked_processes, "TrialResult.leaked_processes must record escaped background process"
        assert result.verdict.leaked_processes, "GraderVerdict.leaked_processes must record escaped background process"
        recorded_pids = {int(item["pid"]) for item in result.verdict.leaked_processes}
        assert daemon_pid in recorded_pids, f"Expected daemon PID {daemon_pid} in {result.verdict.leaked_processes}"
        assert "leaked" in result.verdict.summary.lower()
        assert str(daemon_pid) in result.verdict.summary
    finally:
        pool.close()
        if daemon_pid_file.is_file():
            raw = daemon_pid_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                try:
                    os.kill(int(raw), 9)
                except OSError:
                    pass


def test_detached_env_scrubbed_daemon_using_sandbox_dir_is_killed_and_fails_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K16: Detached double-fork daemon that scrubs env via execve(..., {}) is still caught if cwd or open FD is in sandbox."""
    task = load_builtin_suite()[0]
    task.timeout_sec = 8
    ref_code = task.reference_solution_files["rate_limiter.py"]
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))

    scenarios = [
        (
            "open_fd_with_chdir_root",
            (
                "        for fd in range(3, 256):\n"
                "            try:\n"
                "                os.close(fd)\n"
                "            except OSError:\n"
                "                pass\n"
                "        keep_fd = os.open('rate_limiter.py', os.O_RDONLY)\n"
                "        os.set_inheritable(keep_fd, True)\n"
                "        os.chdir('/')\n"
                "        dn = os.open(os.devnull, os.O_RDWR)\n"
                "        os.dup2(dn, 0)\n"
                "        os.dup2(dn, 1)\n"
                "        os.dup2(dn, 2)\n"
                "        if dn > 2 and dn != keep_fd:\n"
                "            os.close(dn)\n"
            ),
        ),
        (
            "stay_in_sandbox_cwd",
            (
                "        for fd in range(3, 256):\n"
                "            try:\n"
                "                os.close(fd)\n"
                "            except OSError:\n"
                "                pass\n"
                "        dn = os.open(os.devnull, os.O_RDWR)\n"
                "        os.dup2(dn, 0)\n"
                "        os.dup2(dn, 1)\n"
                "        os.dup2(dn, 2)\n"
                "        if dn > 2:\n"
                "            os.close(dn)\n"
            ),
        ),
    ]

    for idx, (label, child_setup) in enumerate(scenarios, start=1):
        sub_dir = tmp_path / label
        bin_dir = sub_dir / "bin"
        bin_dir.mkdir(parents=True)
        daemon_pid_file = sub_dir / "daemon.pid"
        daemon_pid_tmp = sub_dir / "daemon.pid.tmp"

        _make_stub_cli(
            bin_dir,
            "opencode",
            (
                "import os, sys, time\n"
                "from pathlib import Path\n"
                f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
                "pid1 = os.fork()\n"
                "if pid1 == 0:\n"
                "    os.setsid()\n"
                "    pid2 = os.fork()\n"
                "    if pid2 == 0:\n"
                f"{child_setup}"
                f"        Path({str(daemon_pid_tmp)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                f"        os.replace({str(daemon_pid_tmp)!r}, {str(daemon_pid_file)!r})\n"
                "        os.execve(sys.executable, [sys.executable, '-c', 'import time; time.sleep(30)'], {})\n"
                "    os._exit(0)\n"
                "os.waitpid(pid1, 0)\n"
                "for _ in range(50):\n"
                f"    if Path({str(daemon_pid_file)!r}).is_file():\n"
                "        break\n"
                "    time.sleep(0.01)\n"
            ),
        )
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        pool = WorktreeSandboxPool(sub_dir / "pool")
        driver = HarnessDriver(pool, allow_unisolated_host_home=True)
        contender = ContenderConfig(
            contender_id="ctrl",
            label=f"Env-Scrub Stub ({label})",
            role="control",
            cli="opencode",
            model="stub-model",
        )
        try:
            result = driver.run_trial(f"exp_k16_{idx}", task, contender, rep_index=1)
            assert daemon_pid_file.is_file(), f"[{label}] Detached daemon did not record its PID"
            daemon_pid = int(daemon_pid_file.read_text(encoding="utf-8").strip())

            assert not _pid_is_alive(daemon_pid), (
                f"[{label}] Escaped env-scrubbed daemon PID {daemon_pid} survived trial cleanup!"
            )
            assert not result.passed, (
                f"[{label}] Trial leaving env-scrubbed background daemon MUST fail even with passing fix!"
            )
            recorded_pids = {int(item["pid"]) for item in result.verdict.leaked_processes}
            assert daemon_pid in recorded_pids, (
                f"[{label}] Expected daemon PID {daemon_pid} in {result.verdict.leaked_processes}"
            )
            assert "leaked" in result.verdict.summary.lower()
        finally:
            pool.close()
            if daemon_pid_file.is_file():
                raw = daemon_pid_file.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    try:
                        os.kill(int(raw), 9)
                    except OSError:
                        pass
