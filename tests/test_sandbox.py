"""Scoped tests for M1 isolated HOME (K1), agent-relay relay.sh execution (K9), timeout (K5), and K13 daemons."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time

import pytest

from skill_arena.models import ContenderConfig
import skill_arena.runners.harness_driver as harness_driver_mod
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
    script_path.write_text(f"#!{sys.executable}\n{python_body}\n", encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _snapshot_tree(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            if fp.is_symlink():
                snap[rel] = f"symlink:{os.readlink(fp)}"
            elif fp.is_file():
                snap[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return snap


def test_per_trial_isolated_home_control_zero_skills_treatment_only_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set up a fake "host" HOME containing distractor global skills and settings
    fake_host_home = tmp_path / "host_home"
    for distractor in ("host-distractor-a", "host-distractor-b"):
        d = fake_host_home / ".agents" / "skills" / distractor
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {distractor}\n", encoding="utf-8")
    d_claude = fake_host_home / ".claude" / "skills" / "host-claude-distractor"
    d_claude.mkdir(parents=True)
    (d_claude / "SKILL.md").write_text("# host-claude-distractor\n", encoding="utf-8")
    (fake_host_home / ".claude" / "settings.json").write_text('{"theme":"dark"}\n', encoding="utf-8")

    before_claude_snap = _snapshot_tree(fake_host_home / ".claude")
    before_agents_snap = _snapshot_tree(fake_host_home / ".agents" / "skills")

    # Candidate skill for Treatment arm ONLY
    candidate_skill = tmp_path / "candidate-concurrency-guard"
    candidate_skill.mkdir()
    (candidate_skill / "SKILL.md").write_text(
        "# Candidate Concurrency Guard\nUse lock around refill.\n",
        encoding="utf-8",
    )

    # Stub claude dumps its environment and visible global/workspace skills, and writes to $HOME/.claude/probe.txt
    dump_dir = tmp_path / "dumps"
    dump_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "_ = sys.stdin.read()\n"
            "home = Path(os.environ['HOME'])\n"
            "(home / '.claude').mkdir(parents=True, exist_ok=True)\n"
            "(home / '.claude' / 'probe.txt').write_text('isolated-write', encoding='utf-8')\n"
            "def list_skill_names(base: Path) -> list[str]:\n"
            "    found = set()\n"
            "    for rel in ('.agents/skills', '.claude/skills', '.codex/skills', '.config/opencode/skills'):\n"
            "        root = base / rel\n"
            "        if root.exists():\n"
            "            for p in root.rglob('SKILL.md'):\n"
            "                found.add(p.parent.name)\n"
            "    return sorted(found)\n"
            "payload = {\n"
            "    'contender': os.environ.get('SKILL_ARENA_CONTENDER'),\n"
            "    'HOME': str(home),\n"
            "    'XDG_CONFIG_HOME': os.environ.get('XDG_CONFIG_HOME', ''),\n"
            "    'XDG_DATA_HOME': os.environ.get('XDG_DATA_HOME', ''),\n"
            "    'XDG_CACHE_HOME': os.environ.get('XDG_CACHE_HOME', ''),\n"
            "    'CLAUDE_CONFIG_DIR': os.environ.get('CLAUDE_CONFIG_DIR', ''),\n"
            "    'CODEX_HOME': os.environ.get('CODEX_HOME', ''),\n"
            "    'VIBE_HOME': os.environ.get('VIBE_HOME', ''),\n"
            "    'global_skills': list_skill_names(home),\n"
            "    'workspace_skills': list_skill_names(Path.cwd()),\n"
            "    'claude_symlink_resolves': (home / '.claude/skills/candidate-concurrency-guard/SKILL.md').is_file(),\n"
            "    'agents_skill_resolves': (home / '.agents/skills/candidate-concurrency-guard/SKILL.md').is_file(),\n"
            "}\n"
            f"out_file = Path({str(dump_dir)!r}) / (payload['contender'] + '.json')\n"
            "out_file.write_text(json.dumps(payload, indent=2), encoding='utf-8')\n"
            "print(json.dumps({'result': 'Inspected environment.\\nSTATUS: success', 'total_cost_usd': 0.002}))\n"
        ),
    )

    monkeypatch.setenv("HOME", str(fake_host_home))
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    # Include a pre-existing workspace skill in task seed to verify Control strips workspace skills too!
    task = load_builtin_suite()[0]
    task.seed_files[".claude/skills/preexisting-repo-skill/SKILL.md"] = "# Repo leak\n"

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    ctrl = ContenderConfig(
        contender_id="ctrl",
        label="Control",
        role="control",
        cli="claude",
        model="claude-sonnet-4-5",
        skill_paths=[],
    )
    treat = ContenderConfig(
        contender_id="treat",
        label="Treatment",
        role="treatment",
        cli="claude",
        model="claude-sonnet-4-5",
        skill_paths=[str(candidate_skill)],
    )
    try:
        res_ctrl = driver.run_trial("exp_iso", task, ctrl, rep_index=1)
        res_treat = driver.run_trial("exp_iso", task, treat, rep_index=1)
    finally:
        pool.close()

    ctrl_dump = json.loads((dump_dir / "ctrl.json").read_text(encoding="utf-8"))
    treat_dump = json.loads((dump_dir / "treat.json").read_text(encoding="utf-8"))

    # 1. Fresh temporary HOME per trial, distinct from host HOME and cleaned up post-trial
    assert ctrl_dump["HOME"] != str(fake_host_home)
    assert treat_dump["HOME"] != str(fake_host_home)
    assert ctrl_dump["HOME"] != treat_dump["HOME"]
    assert not Path(ctrl_dump["HOME"]).exists(), "Per-trial isolated HOME should be cleaned up after trial"
    assert not Path(treat_dump["HOME"]).exists(), "Per-trial isolated HOME should be cleaned up after trial"
    assert res_ctrl.usage.isolated_home_used is True
    assert res_treat.usage.isolated_home_used is True

    # 2. All XDG & vendor config vars point inside the isolated trial HOME
    for dump in (ctrl_dump, treat_dump):
        h = dump["HOME"]
        assert dump["XDG_CONFIG_HOME"] == f"{h}/.config"
        assert dump["XDG_DATA_HOME"] == f"{h}/.local/share"
        assert dump["XDG_CACHE_HOME"] == f"{h}/.cache"
        assert dump["CLAUDE_CONFIG_DIR"] == f"{h}/.claude"
        assert dump["CODEX_HOME"] == f"{h}/.codex"
        assert dump["VIBE_HOME"] == f"{h}/.vibe"

    # 3. Control sees 0 global skills and 0 workspace skills; Treatment sees ONLY the 1 candidate skill
    assert ctrl_dump["global_skills"] == []
    assert ctrl_dump["workspace_skills"] == []
    assert ctrl_dump["claude_symlink_resolves"] is False
    assert ctrl_dump["agents_skill_resolves"] is False

    assert treat_dump["global_skills"] == ["candidate-concurrency-guard"]
    assert treat_dump["workspace_skills"] == ["candidate-concurrency-guard"]
    assert treat_dump["claude_symlink_resolves"] is True
    assert treat_dump["agents_skill_resolves"] is True

    # 4. Host directories completely untouched
    assert not (fake_host_home / ".claude" / "probe.txt").exists()
    assert _snapshot_tree(fake_host_home / ".claude") == before_claude_snap
    assert _snapshot_tree(fake_host_home / ".agents" / "skills") == before_agents_snap


def test_relay_sh_integration_parses_log_md_strips_relay_worker_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verify custom CLI flag builders were removed from harness_driver
    driver_src = inspect.getsource(harness_driver_mod)
    assert "def build_cli_command" not in driver_src
    assert "def resolve_cli_binary" not in driver_src
    assert "--dangerously-skip-permissions" not in driver_src

    class FakeAnthropicUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            if n > 0:
                self.rfile.read(n)
            body = json.dumps(
                {
                    "id": "msg_relay_1",
                    "model": "claude-sonnet-4-5",
                    "usage": {
                        "input_tokens": 900,
                        "cache_read_input_tokens": 300,
                        "output_tokens": 120,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    up = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropicUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        up_url = f"http://127.0.0.1:{up.server_address[1]}"
        task = load_builtin_suite()[0]
        ref_code = task.reference_solution_files["rate_limiter.py"]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub_claude = _make_stub_cli(
            bin_dir,
            "claude",
            (
                "import json, os, sys, urllib.request\n"
                "from pathlib import Path\n"
                "prompt_txt = sys.stdin.read()\n"
                "assert 'rate_limiter.py' in prompt_txt\n"
                "req = urllib.request.Request(\n"
                "    os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',\n"
                "    data=json.dumps({'model': 'claude-sonnet-4-5', 'messages': [{'role': 'user', 'content': prompt_txt}]}).encode('utf-8'),\n"
                "    headers={'Content-Type': 'application/json'},\n"
                "    method='POST',\n"
                ")\n"
                "with urllib.request.urlopen(req, timeout=5) as resp:\n"
                "    resp.read()\n"
                f"Path('rate_limiter.py').write_text({ref_code!r}, encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'result': 'Applied atomic lock around refill.\\nSTATUS: success',\n"
                "    'total_cost_usd': 0.015,\n"
                "    'session_id': 'stub-relay-1',\n"
                "    'modelUsage': {'claude-sonnet-4-5': {}},\n"
                "}))\n"
            ),
        )

        # Inherited RELAY_WORKER in outer harness must be stripped so relay.sh does not abort!
        monkeypatch.setenv("RELAY_WORKER", "99-outer-worker")
        monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

        pool = WorktreeSandboxPool(tmp_path / "pool")
        driver = HarnessDriver(pool, upstream_api_url=up_url)
        contender = ContenderConfig(
            contender_id="treat",
            label="Claude via Relay",
            role="treatment",
            cli="claude",
            model="claude-sonnet-4-5",
        )
        try:
            result = driver.run_trial("exp_relay", task, contender, rep_index=1)
            assert result.passed is True, result.verdict.summary
            assert result.anti_cheat_clean is True
            assert ".relay" not in result.git_diff
            assert result.usage.is_metered is True
            assert result.usage.first_turn_prompt_tokens == 1200
            assert result.usage.uncached_input_tokens == 900
            assert result.usage.cached_prompt_tokens == 300
            assert result.usage.completion_tokens == 120
            assert result.usage.relay_status == "ok"
            assert result.usage.relay_cost_usd == pytest.approx(0.015, abs=1e-6)

            # Unsupported CLI name must fail cleanly with ValueError
            bad_contender = ContenderConfig(
                contender_id="bad",
                label="Unsupported CLI",
                role="control",
                cli="opencode",
                model="stub",
            )
            with pytest.raises(ValueError, match="agent-relay"):
                driver.run_trial("exp_bad", task, bad_contender, rep_index=1)
        finally:
            pool.close()
    finally:
        up.shutdown()
        up.server_close()


def test_host_home_opt_in_requires_explicit_unisolated_home_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran.txt"
    stub = _make_stub_cli(
        bin_dir,
        "claude",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)  # allow_unisolated_host_home=False
    task = load_builtin_suite()[0]
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Host-Home Claude",
        role="control",
        cli="claude",
        model="stub-model",
        isolate_home=False,
        allow_unisolated_host_home=False,
    )
    try:
        with pytest.raises(PermissionError, match="allow-unisolated-host-home"):
            driver.run_trial("exp_k2", task, contender, rep_index=1)
        assert not marker.exists(), "Agent CLI must never be spawned in host HOME without explicit consent"
    finally:
        pool.close()


def test_run_bounded_command_kills_timed_out_leader_and_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "leader.pid"
    pid_tmp = tmp_path / "leader.pid.tmp"
    code = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_tmp)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"os.replace({str(pid_tmp)!r}, {str(pid_file)!r})\n"
        "time.sleep(30)\n"
    )
    try:
        rc, _, _, timed_out = WorktreeSandboxPool.run_bounded_command(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            timeout_sec=3,
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
    pid_tmp = tmp_path / "sleeper.pid.tmp"
    task = load_builtin_suite()[0]
    task.timeout_sec = 3
    ref_code = task.reference_solution_files["rate_limiter.py"]

    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
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
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Timeout Stub",
        role="control",
        cli="claude",
        model="stub-model",
    )
    t0 = time.perf_counter()
    try:
        result = driver.run_trial("exp_timeout", task, contender, rep_index=1)
        elapsed = time.perf_counter() - t0

        assert elapsed < 15.0, f"Timed-out trial took {elapsed:.2f}s instead of aborting promptly"
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

    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, subprocess\n"
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
            "print(json.dumps({'result': 'stub exiting immediately\\nSTATUS: success'}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Detached Spawner Stub",
        role="control",
        cli="claude",
        model="stub-model",
    )
    t0 = time.perf_counter()
    try:
        result = driver.run_trial("exp_orphan", task, contender, rep_index=1)
        elapsed = time.perf_counter() - t0
        assert elapsed < 6.0, f"Detached background process stalled trial for {elapsed:.2f}s!"
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
    """K13 under isolated HOME: Stub writes passing answer, then double-forks + setsid + closes fds 3..255 + chdir('/')."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    daemon_pid_file = tmp_path / "daemon.pid"
    daemon_pid_tmp = tmp_path / "daemon.pid.tmp"
    home_seen_file = tmp_path / "daemon_home.txt"
    task = load_builtin_suite()[0]
    task.timeout_sec = 8
    ref_code = task.reference_solution_files["rate_limiter.py"]

    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"Path({str(home_seen_file)!r}).write_text(os.environ['HOME'], encoding='utf-8')\n"
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
            "print(json.dumps({'result': 'Done\\nSTATUS: success'}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Double-Fork Daemon Stub",
        role="control",
        cli="claude",
        model="stub-model",
    )
    try:
        result = driver.run_trial("exp_k13", task, contender, rep_index=1)
        assert result.usage.isolated_home_used is True
        assert home_seen_file.is_file()
        assert home_seen_file.read_text(encoding="utf-8") != os.environ.get("HOME", "")
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


def test_k13_sip_sleep_double_fork_cleaned_up_in_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part 2 K13: Verify SIP `/bin/sleep 120` double-fork behavior under isolated HOME and zero leaked test sleepers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sip_pid_file = tmp_path / "sip_daemon.pid"
    sip_pid_tmp = tmp_path / "sip_daemon.pid.tmp"
    task = load_builtin_suite()[0]
    task.timeout_sec = 8

    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, time\n"
            "from pathlib import Path\n"
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
            f"        Path({str(sip_pid_tmp)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            f"        os.replace({str(sip_pid_tmp)!r}, {str(sip_pid_file)!r})\n"
            "        os.execv('/bin/sleep', ['/bin/sleep', '120'])\n"
            "    os._exit(0)\n"
            "os.waitpid(pid1, 0)\n"
            "for _ in range(50):\n"
            f"    if Path({str(sip_pid_file)!r}).is_file():\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "print(json.dumps({'result': 'Spawned SIP sleep.\\nSTATUS: success'}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="SIP Double-Fork Stub",
        role="control",
        cli="claude",
        model="stub-model",
    )
    sip_pid: int | None = None
    try:
        result = driver.run_trial("exp_k13_sip", task, contender, rep_index=1)
        assert result.usage.isolated_home_used is True
        assert sip_pid_file.is_file()
        sip_pid = int(sip_pid_file.read_text(encoding="utf-8").strip())
    finally:
        pool.close()
        if sip_pid_file.is_file():
            raw = sip_pid_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                try:
                    os.kill(int(raw), 9)
                except OSError:
                    pass
                for _ in range(20):
                    if not _pid_is_alive(int(raw)):
                        break
                    time.sleep(0.02)

    assert sip_pid is not None and not _pid_is_alive(sip_pid), "SIP sleep 120 must be cleaned up by test finally!"
