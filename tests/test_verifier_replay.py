"""M2 Stage 1 tests for repository verifier: clean clone, env guard, build/test detection, README replay, and deterministic reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

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


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _basic_repo_files(readme_md: str = "# Demo\n\n```bash\npython3 tool.py --version\n```\n") -> dict[str, str]:
    return {
        "tool.py": (
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('demo-tool 0.1.0')\n"
            "elif '--lang' in sys.argv:\n"
            "    idx = sys.argv.index('--lang')\n"
            "    lang = sys.argv[idx + 1]\n"
            "    print('你好' if lang == 'zh' else 'hello')\n"
            "else:\n"
            "    print('ok')\n"
        ),
        "tests/test_tool.py": (
            "import subprocess, sys, unittest\n\n"
            "class ToolTests(unittest.TestCase):\n"
            "    def test_version(self) -> None:\n"
            "        out = subprocess.check_output([sys.executable, 'tool.py', '--version'], text=True)\n"
            "        self.assertEqual(out.strip(), 'demo-tool 0.1.0')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
        "README.md": readme_md,
    }


def test_verify_stage2_flags_do_not_fake_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "demo_repo"
    _init_git_repo(repo, _basic_repo_files())

    # Default (--mutate enabled by default) must not fake-pass in Stage 1
    rc_default = main(["--lang", "zh", "verify", "--repo", str(repo)])
    cap_default = capsys.readouterr()
    assert rc_default == 2
    assert "第 2 段" in (cap_default.out + cap_default.err)

    rc_mutate_en = main(["--lang", "en", "verify", "--repo", str(repo), "--mutate"])
    cap_mutate_en = capsys.readouterr()
    assert rc_mutate_en == 2
    assert "stage 2" in (cap_mutate_en.out + cap_mutate_en.err).lower()

    self_report = tmp_path / "self-report.md"
    self_report.write_text("# Report\n全部通过。\n", encoding="utf-8")
    rc_sr = main(["--lang", "zh", "verify", "--self-report", str(self_report)])
    cap_sr = capsys.readouterr()
    assert rc_sr == 2
    assert "第 2 段" in (cap_sr.out + cap_sr.err)


def test_verify_invalid_inputs_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Neither --repo nor --self-report
    rc_none = main(["--lang", "en", "verify", "--no-mutate"])
    capsys.readouterr()
    assert rc_none == 2

    # Missing directory
    rc_missing = main(["--lang", "en", "verify", "--repo", str(tmp_path / "no_such_repo"), "--no-mutate"])
    capsys.readouterr()
    assert rc_missing == 2

    # Plain directory (not a Git repository)
    plain = tmp_path / "plain_dir"
    plain.mkdir()
    rc_plain = main(["--lang", "zh", "verify", "--repo", str(plain), "--no-mutate"])
    cap_plain = capsys.readouterr()
    assert rc_plain == 2
    assert "Git" in (cap_plain.out + cap_plain.err) or "仓库" in (cap_plain.out + cap_plain.err)

    # Invalid --ref inside a valid Git repository
    repo = tmp_path / "valid_repo"
    _init_git_repo(repo, _basic_repo_files())
    rc_bad_ref = main(["--lang", "en", "verify", "--repo", str(repo), "--ref", "deadbeef9999", "--no-mutate"])
    capsys.readouterr()
    assert rc_bad_ref == 2


def test_clean_clone_ignores_untracked_secrets_and_preserves_dot_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skill_arena.verifier.env_guard import repo_fingerprint

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("OPENCODE_API_KEY", "host-secret-should-never-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret-should-never-leak")

    files = {
        "check_env.py": (
            "import os, sys\n"
            "from pathlib import Path\n"
            "assert (Path.cwd() / '.git').exists(), '.git missing (must use git clone, not git archive)'\n"
            "assert not (Path.cwd() / '.env').exists(), 'untracked .env leaked (must not use cp -R)'\n"
            f"assert os.environ['HOME'] != {real_home!r}, 'HOME was not isolated'\n"
            "assert os.environ.get('UV_OFFLINE') == '1', 'UV_OFFLINE missing'\n"
            "assert 'OPENCODE_API_KEY' not in os.environ, 'OPENCODE_API_KEY leaked'\n"
            "assert 'OPENAI_API_KEY' not in os.environ, 'OPENAI_API_KEY leaked'\n"
            "print('env-isolation-ok')\n"
        ),
        "tests/test_env.py": (
            "import subprocess, sys, unittest\n\n"
            "class EnvGuardTests(unittest.TestCase):\n"
            "    def test_isolated_clean_clone(self) -> None:\n"
            "        rc = subprocess.call([sys.executable, 'check_env.py'])\n"
            "        self.assertEqual(rc, 0)\n"
        ),
        "README.md": "# Isolation Check\n\n```bash\npython3 check_env.py\n```\n",
    }
    repo = tmp_path / "secret_repo"
    _init_git_repo(repo, files)
    # Untracked .env in source repo (mirrors diy-harness root .env)
    (repo / ".env").write_text("OPENCODE_API_KEY=untracked_secret\n", encoding="utf-8")

    fp_before = repo_fingerprint(repo)
    json_out = tmp_path / "report.json"
    rc = main(["--lang", "en", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    fp_after = repo_fingerprint(repo)

    assert rc == 0
    assert fp_before == fp_after
    assert (repo / ".env").read_text(encoding="utf-8") == "OPENCODE_API_KEY=untracked_secret\n"
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "PASS"
    assert data["env_guard"]["source_repo_untouched"] is True
    assert data["env_guard"]["temp_copy_deleted"] is True


def test_bilingual_readme_parity_consistent_passes_and_ignores_subdir_readmes(tmp_path: Path) -> None:
    files = _basic_repo_files(
        readme_md=(
            "# Demo\n\n"
            "```bash\n"
            "python3 tool.py --lang en\n"
            "python3 tool.py --version\n"
            "```\n"
        )
    )
    files["README.zh-CN.md"] = (
        "# 演示\n\n"
        "```bash\n"
        "python3 tool.py --lang zh\n"
        "python3 tool.py --version\n"
        "```\n"
    )
    # Red-team subdirectory fixture README must NEVER be read or executed
    files["evals-redteam/tasks/rt-readme-injection/fixture/README.md"] = (
        "# Trap\n\n```bash\npython3 -c \"import sys; sys.exit(99)\"\n```\n"
    )
    repo = tmp_path / "bilingual_ok"
    _init_git_repo(repo, files)

    json_out = tmp_path / "bilingual_ok.json"
    rc = main(["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    assert rc == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "PASS"
    assert data["readme_replay"]["bilingual_parity"]["status"] == "PASS"
    assert data["readme_replay"]["discovered_readmes"] == ["README.md", "README.zh-CN.md"]


def test_bilingual_readme_parity_mismatch_fails(tmp_path: Path) -> None:
    files = _basic_repo_files(
        readme_md="# Demo\n\n```bash\npython3 tool.py --version\npython3 tool.py --lang en\n```\n"
    )
    files["README.zh-CN.md"] = "# 演示\n\n```bash\npython3 tool.py --version\n```\n"
    repo = tmp_path / "bilingual_bad"
    _init_git_repo(repo, files)

    json_out = tmp_path / "bilingual_bad.json"
    rc = main(["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    assert rc == 1
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "FAIL"
    assert data["readme_replay"]["bilingual_parity"]["status"] == "FAIL"


def test_readme_unverifiable_categories_and_non_command_blocks(tmp_path: Path) -> None:
    readme = (
        "# Categories Demo\n\n"
        "```toml\n"
        "[section]\n"
        "mode = \"auto\"\n"
        "```\n\n"
        "```bash\n"
        "uv tool install --editable .\n"
        "pip install requests\n"
        "export OPENAI_API_KEY=...\n"
        "python3 tool.py --thread <thread_id>\n"
        "python3 tool.py \"...\"\n"
        "cp .env ~/.demo-tool/.env\n"
        "python3 tool.py serve --port 8787\n"
        "docker build -t demo .\n"
        "git tag v0.2.0 && git push origin main v0.2.0\n"
        "python3 tool.py --version\n"
        "```\n"
    )
    repo = tmp_path / "cats_repo"
    _init_git_repo(repo, _basic_repo_files(readme_md=readme))

    json_out = tmp_path / "cats.json"
    md_out = tmp_path / "cats.md"
    rc = main(
        [
            "--lang",
            "zh",
            "verify",
            "--repo",
            str(repo),
            "--no-mutate",
            "--json-out",
            str(json_out),
            "--md-out",
            str(md_out),
        ]
    )
    assert rc == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "PASS"
    assert data["readme_replay"]["non_command_block_count"] == 1

    items_by_cmd = {item["command"]: item for item in data["readme_replay"]["items"]}
    assert items_by_cmd["uv tool install --editable ."]["category"] == "UNVERIFIABLE_OFFLINE"
    assert "离线" in items_by_cmd["uv tool install --editable ."]["reason"]
    assert items_by_cmd["pip install requests"]["category"] == "UNVERIFIABLE_OFFLINE"
    assert items_by_cmd["export OPENAI_API_KEY=..."]["category"] == "UNVERIFIABLE_CREDENTIALS"
    assert items_by_cmd["python3 tool.py --thread <thread_id>"]["category"] == "UNVERIFIABLE_PLACEHOLDER"
    assert items_by_cmd['python3 tool.py "..."']["category"] == "UNVERIFIABLE_PLACEHOLDER"
    assert items_by_cmd["cp .env ~/.demo-tool/.env"]["category"] == "UNVERIFIABLE_USER_HOME"
    assert items_by_cmd["python3 tool.py serve --port 8787"]["category"] == "UNVERIFIABLE_DAEMON"
    assert items_by_cmd["docker build -t demo ."]["category"] == "UNVERIFIABLE_DOCKER"
    assert items_by_cmd["git tag v0.2.0 && git push origin main v0.2.0"]["category"] == "UNVERIFIABLE_RELEASE_MUTATION"
    assert items_by_cmd["python3 tool.py --version"]["category"] == "EXECUTED_PASS"
    assert items_by_cmd["python3 tool.py --version"]["status"] == "PASS"
    assert items_by_cmd["python3 tool.py --version"]["exit_code"] == 0
    assert "无法验证（离线）" in md_out.read_text(encoding="utf-8")


def test_unstated_precondition_command_fails_unless_skip_cmd_exempts(tmp_path: Path) -> None:
    files = _basic_repo_files(
        readme_md=(
            "# Precondition Demo\n\n"
            "```bash\n"
            "python3 tool.py --version\n"
            "python3 sync_external.py --check\n"
            "```\n"
        )
    )
    files["sync_external.py"] = (
        "import os, pathlib, sys\n"
        "ext = pathlib.Path(os.environ['HOME']) / 'deepagents'\n"
        "if not ext.exists():\n"
        "    print(f'fatal: missing {ext}', file=sys.stderr)\n"
        "    sys.exit(2)\n"
    )
    repo = tmp_path / "precond_repo"
    _init_git_repo(repo, files)

    # Without --skip-cmd: must fail (exit 1)
    j_fail = tmp_path / "fail.json"
    rc_fail = main(["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(j_fail)])
    assert rc_fail == 1
    d_fail = json.loads(j_fail.read_text(encoding="utf-8"))
    assert d_fail["conclusion"] == "FAIL"
    sync_item = next(i for i in d_fail["readme_replay"]["items"] if "sync_external.py" in i["command"])
    assert sync_item["status"] == "FAIL"
    assert sync_item["category"] == "EXECUTED_FAIL"
    assert sync_item["exit_code"] == 2

    # With explicit --skip-cmd: passes (exit 0) and lists exemption in report
    j_skip = tmp_path / "skip.json"
    m_skip = tmp_path / "skip.md"
    rc_skip = main(
        [
            "--lang",
            "zh",
            "verify",
            "--repo",
            str(repo),
            "--no-mutate",
            "--skip-cmd",
            "sync_external.py",
            "--json-out",
            str(j_skip),
            "--md-out",
            str(m_skip),
        ]
    )
    assert rc_skip == 0
    d_skip = json.loads(j_skip.read_text(encoding="utf-8"))
    assert d_skip["conclusion"] == "PASS"
    sync_skipped = next(i for i in d_skip["readme_replay"]["items"] if "sync_external.py" in i["command"])
    assert sync_skipped["category"] == "SKIPPED_USER"
    assert "sync_external.py" in m_skip.read_text(encoding="utf-8")


def test_zero_tests_ran_fails_verification(tmp_path: Path) -> None:
    # 1. unittest repository that runs 0 tests (unittest exits 0 on 0 tests!)
    u_repo = tmp_path / "zero_unittest"
    _init_git_repo(
        u_repo,
        {
            "tool.py": "print('ok')\n",
            "tests/test_nothing.py": "import unittest\n\nclass EmptyCase(unittest.TestCase):\n    pass\n",
            "README.md": "# Zero Tests\n\n```bash\npython3 tool.py\n```\n",
        },
    )
    j_unit = tmp_path / "zero_unit.json"
    rc_unit = main(["--lang", "zh", "verify", "--repo", str(u_repo), "--no-mutate", "--json-out", str(j_unit)])
    assert rc_unit == 1
    d_unit = json.loads(j_unit.read_text(encoding="utf-8"))
    assert d_unit["build_and_test"]["test"]["status"] == "FAIL"
    assert d_unit["build_and_test"]["test"]["zero_tests_ran"] is True

    # 2. pytest repository that collects 0 items
    p_repo = tmp_path / "zero_pytest"
    _init_git_repo(
        p_repo,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "tests/helper.py": "VALUE = 1\n",
            "README.md": "# Zero Pytest\n\n```bash\npython3 -c \"print('hi')\"\n```\n",
        },
    )
    j_py = tmp_path / "zero_py.json"
    rc_py = main(["--lang", "en", "verify", "--repo", str(p_repo), "--no-mutate", "--json-out", str(j_py)])
    assert rc_py == 1
    d_py = json.loads(j_py.read_text(encoding="utf-8"))
    assert d_py["build_and_test"]["test"]["status"] == "FAIL"
    assert d_py["build_and_test"]["test"]["zero_tests_ran"] is True


def test_readonly_files_and_dirs_in_temp_copy_are_cleaned_up(tmp_path: Path) -> None:
    files = _basic_repo_files(
        readme_md="# ReadOnly Cleanup\n\n```bash\npython3 make_readonly.py\n```\n"
    )
    files["make_readonly.py"] = (
        "import os, pathlib\n"
        "for root in (pathlib.Path.cwd(), pathlib.Path(os.environ['HOME'])):\n"
        "    ro_dir = root / 'ro_vault'\n"
        "    ro_dir.mkdir(parents=True, exist_ok=True)\n"
        "    ro_file = ro_dir / 'locked.txt'\n"
        "    ro_file.write_text('immutable', encoding='utf-8')\n"
        "    os.chmod(ro_file, 0o444)\n"
        "    os.chmod(ro_dir, 0o555)\n"
        "print('created-readonly')\n"
    )
    repo = tmp_path / "ro_repo"
    _init_git_repo(repo, files)

    json_out = tmp_path / "ro.json"
    rc = main(["--lang", "en", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    assert rc == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["env_guard"]["temp_copy_deleted"] is True


def test_leaked_background_process_reaped_and_marked_failed(tmp_path: Path) -> None:
    pid_record = tmp_path / "spawned_daemon.pid"
    files = _basic_repo_files(
        readme_md="# Leaked Daemon\n\n```bash\npython3 spawn_daemon.py\n```\n"
    )
    files["spawn_daemon.py"] = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(pid_record)!r}, 'w', encoding='utf-8').write(str(p.pid))\n"
        "print('spawned')\n"
    )
    repo = tmp_path / "leak_repo"
    _init_git_repo(repo, files)

    json_out = tmp_path / "leak.json"
    rc = main(["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    assert pid_record.is_file()
    leaked_pid = int(pid_record.read_text(encoding="utf-8").strip())

    assert not _pid_is_alive(leaked_pid), f"Escaped process {leaked_pid} was not reaped!"
    assert rc == 1
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["conclusion"] == "FAIL"
    assert data["env_guard"]["no_leaked_processes"] is False
    assert data["env_guard"]["leaked_process_count"] >= 1


def test_source_repo_fingerprint_mismatch_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skill_arena.verifier.runner as runner_mod
    from skill_arena.verifier.env_guard import RepoFingerprint, repo_fingerprint

    repo = tmp_path / "tamper_repo"
    _init_git_repo(repo, _basic_repo_files())

    calls = 0

    def _tampered_fp(path: Path) -> RepoFingerprint:
        nonlocal calls
        calls += 1
        real = repo_fingerprint(path)
        if calls >= 2:
            return RepoFingerprint(
                head_sha=real.head_sha,
                index_mtime_ns=(real.index_mtime_ns or 0) + 1,
                index_sha256="tampered",
                status_sha256=real.status_sha256,
                refs_sha256=real.refs_sha256,
            )
        return real

    monkeypatch.setattr(runner_mod, "repo_fingerprint", _tampered_fp)
    json_out = tmp_path / "tamper.json"
    rc = main(["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(json_out)])
    assert rc == 2
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["env_guard"]["source_repo_untouched"] is False
    assert data["env_guard"]["temp_copy_deleted"] is True


def test_two_runs_json_identical_without_timing_meta_and_paths_scrubbed(tmp_path: Path) -> None:
    from skill_arena.verifier.env_guard import repo_fingerprint

    repo = tmp_path / "det_repo"
    _init_git_repo(repo, _basic_repo_files())

    fp0 = repo_fingerprint(repo)
    j1, m1 = tmp_path / "run1.json", tmp_path / "run1.md"
    j2, m2 = tmp_path / "run2.json", tmp_path / "run2.md"

    rc1 = main(
        ["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(j1), "--md-out", str(m1)]
    )
    rc2 = main(
        ["--lang", "zh", "verify", "--repo", str(repo), "--no-mutate", "--json-out", str(j2), "--md-out", str(m2)]
    )
    fp1 = repo_fingerprint(repo)

    assert rc1 == 0
    assert rc2 == 0
    assert fp0 == fp1

    d1 = json.loads(j1.read_text(encoding="utf-8"))
    d2 = json.loads(j2.read_text(encoding="utf-8"))
    assert "timing_meta" in d1 and "timing_meta" in d2
    d1_det = {k: v for k, v in d1.items() if k != "timing_meta"}
    d2_det = {k: v for k, v in d2.items() if k != "timing_meta"}
    assert json.dumps(d1_det, indent=2, ensure_ascii=False) == json.dumps(d2_det, indent=2, ensure_ascii=False)
    assert m1.read_text(encoding="utf-8") == m2.read_text(encoding="utf-8")

    real_home = pwd.getpwuid(os.getuid()).pw_dir
    for txt in (j1.read_text(encoding="utf-8"), m1.read_text(encoding="utf-8")):
        assert str(repo) not in txt
        assert real_home not in txt
        assert "sa-verify-" not in txt
