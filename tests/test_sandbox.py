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

from skill_arena.models import ContenderConfig, TrialResult
import skill_arena.runners.harness_driver as harness_driver_mod
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

    # 4. Host directories do not contain trial marker writes
    assert not (fake_host_home / ".claude" / "probe.txt").exists()


def test_kr1_marker_isolation_passes_during_concurrent_real_claude_writes_and_no_directory_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    m1_script = (repo_root / "Scripts" / "acceptance" / "m1-acceptance.sh").read_text(encoding="utf-8")
    assert "def snapshot_tree" not in m1_script, "KR1: full-directory snapshot_tree must be removed from m1-acceptance.sh"
    assert "kr1_probe_" in m1_script, "KR1: m1-acceptance.sh must verify unique kr1_probe_<uuid> markers"

    real_home = tmp_path / "real_home"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".agents" / "skills").mkdir(parents=True)
    live_session_file = real_home / ".claude" / "live_session.jsonl"
    live_session_file.write_text('{"seq": 0}\n', encoding="utf-8")

    probe_uuid = "7f8e9d0c1b2a"
    probe_report = tmp_path / "kr1_probe_report.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "_ = sys.stdin.read()\n"
            "home = Path(os.environ['HOME'])\n"
            f"claude_marker = home / '.claude' / 'kr1_probe_{probe_uuid}.marker'\n"
            f"skill_marker = home / '.agents' / 'skills' / 'kr1_probe_{probe_uuid}' / 'SKILL.md'\n"
            "claude_marker.parent.mkdir(parents=True, exist_ok=True)\n"
            "skill_marker.parent.mkdir(parents=True, exist_ok=True)\n"
            "claude_marker.write_text('isolated-claude-marker\\n', encoding='utf-8')\n"
            "skill_marker.write_text('# isolated-skill-marker\\n', encoding='utf-8')\n"
            "time.sleep(0.15)\n"
            "report = {\n"
            "    'trial_home': str(home),\n"
            "    'claude_marker_written': claude_marker.is_file(),\n"
            "    'skill_marker_written': skill_marker.is_file(),\n"
            "}\n"
            f"Path({str(probe_report)!r}).write_text(json.dumps(report), encoding='utf-8')\n"
            "print(json.dumps({'result': 'KR1 probe written.\\nSTATUS: success', 'total_cost_usd': 0.001}))\n"
        ),
    )

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    stop_writer = threading.Event()
    writes_done = 0

    def _concurrent_claude_session_writer() -> None:
        nonlocal writes_done
        seq = 1
        while not stop_writer.is_set():
            with live_session_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": seq}) + "\n")
            writes_done += 1
            seq += 1
            time.sleep(0.02)

    writer_thread = threading.Thread(target=_concurrent_claude_session_writer, daemon=True)
    writer_thread.start()

    task = load_builtin_suite()[0]
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    ctrl = ContenderConfig(
        contender_id="ctrl_kr1",
        label="Control",
        role="control",
        cli="claude",
        model="claude-sonnet-4-5",
        skill_paths=[],
    )
    try:
        trial = driver.run_trial("exp_kr1", task, ctrl, rep_index=1)
    finally:
        stop_writer.set()
        writer_thread.join(timeout=2.0)
        pool.close()

    assert writes_done >= 2, "Expected concurrent live_session.jsonl writes during trial"
    obs = json.loads(probe_report.read_text(encoding="utf-8"))
    assert obs["claude_marker_written"] is True
    assert obs["skill_marker_written"] is True
    assert trial.usage.isolated_home_used is True
    assert Path(obs["trial_home"]).resolve() != real_home.resolve()
    assert not Path(obs["trial_home"]).exists()
    assert not (real_home / ".claude" / f"kr1_probe_{probe_uuid}.marker").exists()
    assert not (real_home / ".agents" / "skills" / f"kr1_probe_{probe_uuid}").exists()


def test_kr4_strict_child_env_allowlist_blocks_ssh_claude_code_and_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_host_home = tmp_path / "fake_host_home"
    fake_host_home.mkdir()
    host_tmpdir = tmp_path / "host_tmpdir"
    host_tmpdir.mkdir()
    env_dump_path = tmp_path / "child_env_dump.json"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "_ = sys.stdin.read()\n"
            "env_copy = dict(os.environ)\n"
            "tmpdir_path = Path(env_copy.get('TMPDIR', ''))\n"
            "payload = {\n"
            "    'env': env_copy,\n"
            "    'tmpdir_exists_during_trial': tmpdir_path.is_dir() if env_copy.get('TMPDIR') else False,\n"
            "    'home_exists_during_trial': Path(env_copy.get('HOME', '')).is_dir(),\n"
            "}\n"
            f"Path({str(env_dump_path)!r}).write_text(json.dumps(payload, indent=2), encoding='utf-8')\n"
            "print(json.dumps({'result': 'Dumped env.\\nSTATUS: success', 'total_cost_usd': 0.001}))\n"
        ),
    )

    monkeypatch.setenv("HOME", str(fake_host_home))
    monkeypatch.setenv("TMPDIR", str(host_tmpdir))
    monkeypatch.setenv("TEMP", str(host_tmpdir))
    monkeypatch.setenv("TMP", str(host_tmpdir))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-ssh.sock")
    monkeypatch.setenv("SSH_AGENT_PID", "999")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secret-msg-token")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-12345")
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", "/bin/claude")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("RANDOM_HOST_SECRET", "leak-me-not")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-allowed-key")
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    task = load_builtin_suite()[0]
    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    ctrl = ContenderConfig(
        contender_id="ctrl_kr4",
        label="Control",
        role="control",
        cli="claude",
        model="claude-sonnet-4-5",
        skill_paths=[],
    )
    try:
        driver.run_trial("exp_kr4", task, ctrl, rep_index=1)
    finally:
        pool.close()

    dumped = json.loads(env_dump_path.read_text(encoding="utf-8"))
    child_env: dict[str, str] = dumped["env"]

    assert "SSH_AUTH_SOCK" not in child_env
    assert "SSH_AGENT_PID" not in child_env
    assert "CLAUDECODE" not in child_env
    assert [k for k in child_env if k.startswith("CLAUDE_CODE_")] == []
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert "RANDOM_HOST_SECRET" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env

    # Isolated paths ARE present while un-named provider API keys are NOT forwarded
    assert dumped["home_exists_during_trial"] is True
    assert dumped["tmpdir_exists_during_trial"] is True
    assert Path(child_env["HOME"]).resolve() != fake_host_home.resolve()
    assert Path(child_env["TMPDIR"]).resolve() != host_tmpdir.resolve()
    assert child_env["TEMP"] == child_env["TMPDIR"]
    assert child_env["TMP"] == child_env["TMPDIR"]
    assert child_env.get("XDG_STATE_HOME") == f"{child_env['HOME']}/.local/state"
    assert not Path(child_env["HOME"]).exists(), "Isolated HOME must be cleaned up post-trial"
    assert not Path(child_env["TMPDIR"]).exists(), "Isolated TMPDIR must be cleaned up post-trial"


def test_kr4_default_no_auth_copy_leaves_zero_credentials_in_isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skill_arena.cli import main

    fake_host_home = tmp_path / "host_with_creds"
    seeded_creds = (
        ".claude/.credentials.json",
        ".claude.json",
        ".codex/auth.json",
        ".config/opencode/auth.json",
        ".vibe/.env",
    )
    for rel in seeded_creds:
        p = fake_host_home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'{{"secret_for": "{rel}"}}\n', encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_host_home))
    monkeypatch.setattr("skill_arena.sandbox.skill_injector._real_account_home", lambda: fake_host_home)

    cred_obs_dir = tmp_path / "cred_obs"
    cred_obs_dir.mkdir()
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
            f"candidates = {list(seeded_creds)!r}\n"
            "present = [rel for rel in candidates if (home / rel).exists()]\n"
            "all_files = sorted(\n"
            "    str(p.relative_to(home)) for p in home.rglob('*') if p.is_file() or p.is_symlink()\n"
            ")\n"
            "contender = os.environ.get('SKILL_ARENA_CONTENDER', 'unknown')\n"
            "payload = {\n"
            "    'trial_home': str(home),\n"
            "    'present_credentials': present,\n"
            "    'all_files_in_home': all_files,\n"
            "    'ssh_auth_sock_present': 'SSH_AUTH_SOCK' in os.environ,\n"
            "}\n"
            f"(Path({str(cred_obs_dir)!r}) / f'{{contender}}.json').write_text(json.dumps(payload), encoding='utf-8')\n"
            "print(json.dumps({'result': 'Checked creds.\\nSTATUS: success', 'total_cost_usd': 0.001}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/should-not-leak.sock")

    suite_file = tmp_path / "one_task.json"
    t0 = load_builtin_suite()[0]
    suite_file.write_text(json.dumps([t0.to_dict()]), encoding="utf-8")
    skill_dir = tmp_path / "dummy-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Dummy Skill\n", encoding="utf-8")
    db_path = tmp_path / "kr4_default.sqlite"

    rc = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(suite_file),
            "--skill",
            str(skill_dir),
            "--reps",
            "1",
            "--db",
            str(db_path),
        ]
    )
    assert rc == 0

    obs_files = sorted(cred_obs_dir.glob("*.json"))
    assert len(obs_files) == 2
    for obs_file in obs_files:
        obs = json.loads(obs_file.read_text(encoding="utf-8"))
        assert obs["present_credentials"] == [], (
            f"Default run (no --copy-auth) leaked credentials into isolated HOME: {obs}"
        )
        assert obs["ssh_auth_sock_present"] is False
    ctrl_obs = json.loads((cred_obs_dir / "arm_control.json").read_text(encoding="utf-8"))
    assert ctrl_obs["all_files_in_home"] == []


def test_kr4_readmes_disclose_host_env_only_isolation_boundary_bilingually() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "getpwuid" in readme_en
    assert "environment variables" in readme_en.lower()
    assert "outside the worktree" in readme_en.lower()
    assert "AgentVM" in readme_en

    assert "getpwuid" in readme_zh
    assert "环境变量" in readme_zh
    assert "worktree" in readme_zh or "工作区以外" in readme_zh
    assert "AgentVM" in readme_zh


def test_kr6_zero_api_keys_forwarded_by_default_and_pass_env_forwards_only_named_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KR6: Default forwards ZERO provider API keys; --pass-env forwards ONLY explicitly named host vars."""
    from skill_arena.cli import main
    from skill_arena.runners.harness_driver import _build_allowlisted_trial_env

    api_vars = {
        "ANTHROPIC_API_KEY": "fake-ant-key",
        "OPENAI_API_KEY": "fake-oai-key",
        "MISTRAL_API_KEY": "fake-mis-key",
        "GOOGLE_API_KEY": "fake-goo-key",
        "GEMINI_API_KEY": "fake-gem-key",
        "CUSTOM_VENDOR_API_KEY": "fake-custom-key",
        "GITHUB_TOKEN": "fake-gh-token",
        "GH_TOKEN": "fake-gh-cli-token",
        "NPM_TOKEN": "fake-npm-token",
        "ANTHROPIC_AUTH_TOKEN": "fake-ant-auth-token",
    }
    for k, v in api_vars.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MISSING_NAMED_API_KEY", raising=False)

    # 1. Direct unit check on _build_allowlisted_trial_env default (no pass_env)
    unit_default_env = _build_allowlisted_trial_env({"HOME": str(tmp_path / "h")}, tmp_path / "t")
    leaked_unit = [
        k for k in unit_default_env if k in api_vars or k.endswith("_API_KEY") or k.endswith("_TOKEN")
    ]
    assert leaked_unit == [], f"Default _build_allowlisted_trial_env leaked API keys/tokens: {leaked_unit}"

    # 2. End-to-end CLI check: default run (no --pass-env) forwards ZERO API key or token vars to agent subprocess
    fake_host_home = tmp_path / "host_home"
    fake_host_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_host_home))

    obs_dir = tmp_path / "kr6_obs"
    obs_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_claude = _make_stub_cli(
        bin_dir,
        "claude",
        (
            "import json, os, sys, uuid\n"
            "from pathlib import Path\n"
            "_ = sys.stdin.read()\n"
            "env_map = dict(os.environ)\n"
            f"(Path({str(obs_dir)!r}) / f'env_{{uuid.uuid4().hex[:8]}}.json').write_text(json.dumps(env_map), encoding='utf-8')\n"
            "print(json.dumps({'result': 'Checked env.\\nSTATUS: success', 'total_cost_usd': 0.001}))\n"
        ),
    )
    monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))

    suite_file = tmp_path / "one_task.json"
    suite_file.write_text(json.dumps([load_builtin_suite()[0].to_dict()]), encoding="utf-8")

    rc_default = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(suite_file),
            "--reps",
            "1",
            "--db",
            str(tmp_path / "default.sqlite"),
        ]
    )
    capsys.readouterr()
    assert rc_default == 0
    default_dumps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(obs_dir.glob("*.json"))]
    assert len(default_dumps) == 2
    for d in default_dumps:
        # SKILL_ARENA_TRIAL_TOKEN is SkillArena's own per-trial UUID marker for K13/K16 daemon reaping.
        present_keys = [
            k
            for k in d
            if k != "SKILL_ARENA_TRIAL_TOKEN"
            and (k in api_vars or k.endswith("_API_KEY") or k.endswith("_TOKEN"))
        ]
        assert present_keys == [], f"CLI default run forwarded un-named API keys/tokens: {present_keys}"

    # 3. Explicit --pass-env (repeatable + comma-separated + missing host var) forwards ONLY named present vars
    for p in obs_dir.glob("*.json"):
        p.unlink()

    rc_named = main(
        [
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--suite",
            str(suite_file),
            "--reps",
            "1",
            "--pass-env",
            "OPENAI_API_KEY",
            "--pass-env",
            "MISTRAL_API_KEY,MISSING_NAMED_API_KEY",
            "--db",
            str(tmp_path / "named.sqlite"),
        ]
    )
    capsys.readouterr()
    assert rc_named == 0
    named_dumps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(obs_dir.glob("*.json"))]
    assert len(named_dumps) == 2
    for d in named_dumps:
        assert d.get("OPENAI_API_KEY") == "fake-oai-key"
        assert d.get("MISTRAL_API_KEY") == "fake-mis-key"
        assert "MISSING_NAMED_API_KEY" not in d
        assert "ANTHROPIC_API_KEY" not in d
        assert "GOOGLE_API_KEY" not in d
        assert "GEMINI_API_KEY" not in d
        assert "CUSTOM_VENDOR_API_KEY" not in d
        assert "GITHUB_TOKEN" not in d

    # 4. Invalid or wildcard/assignment/forbidden --pass-env values fail cleanly with exit code 2
    for bad_arg in ("", "*_API_KEY", "OPENAI_API_KEY=foo", "SSH_AUTH_SOCK", "CLAUDE_CODE_SESSION_ID", "HOME"):
        rc_bad = main(
            [
                "--lang",
                "en",
                "run",
                "--cli",
                "claude",
                "--model",
                "claude-sonnet-4-5",
                "--suite",
                str(suite_file),
                "--reps",
                "1",
                "--pass-env",
                bad_arg,
                "--db",
                str(tmp_path / "bad.sqlite"),
            ]
        )
        capsys.readouterr()
        assert rc_bad == 2, f"Expected exit code 2 for invalid --pass-env {bad_arg!r}, got {rc_bad}"


def test_kr6_readmes_accurately_describe_zero_default_api_keys_and_copy_auth() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_zh = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")

    # Old misleading phrasing must be removed
    assert "and host credentials unless `--copy-auth` is passed" not in readme_en

    # Both READMEs must disclose --pass-env for API key env vars AND --copy-auth for CLI auth files
    assert "--pass-env" in readme_en
    assert "--copy-auth" in readme_en
    assert "OPENAI_API_KEY" in readme_en or "API key" in readme_en

    assert "--pass-env" in readme_zh
    assert "--copy-auth" in readme_zh
    assert "API key" in readme_zh or "密钥" in readme_zh


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

    gated_time = _PidFileGatedTime()
    monkeypatch.setattr("skill_arena.sandbox.worktree_pool.time", gated_time)

    pool = WorktreeSandboxPool(tmp_path / "pool")
    driver = HarnessDriver(pool)
    contender = ContenderConfig(
        contender_id="ctrl",
        label="Timeout Stub",
        role="control",
        cli="claude",
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

        stub_claude = _make_stub_cli(
            bin_dir,
            "claude",
            (
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "_ = sys.stdin.read()\n"
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
                "print(json.dumps({'result': 'Done\\nSTATUS: success'}))\n"
            ),
        )
        monkeypatch.setenv("RELAY_CLAUDE_BIN", str(stub_claude))
        pool = WorktreeSandboxPool(sub_dir / "pool")
        driver = HarnessDriver(pool)
        contender = ContenderConfig(
            contender_id="ctrl",
            label=f"Env-Scrub Stub ({label})",
            role="control",
            cli="claude",
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
