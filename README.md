# SkillArena

**English** | **[简体中文](README.zh-CN.md)**

**Controlled Git-worktree & isolated-`HOME` A/B benchmark harness** for coding agents and `SKILL.md` rules, featuring per-trial temporary `HOME` isolation, unified headless agent execution via `agent-relay` (`relay.sh`), streaming HTTP/SSE & OpenAI Responses token metering via `agent-xray`, Newcombe-Wilson 95% confidence intervals (`Inconclusive` / `看不出差别` when noisy), clean-checkout deterministic unit & Hallmark craft grading, SHA-256 anti-cheat verification, and a bilingual (`中文 | EN`) dual-track trajectory replay Studio.

> **Release status**: Latest tagged release is `v0.2.0`. Capabilities marked **Unreleased (after `main` / `v0.2.0`)** below—including per-trial isolated `HOME` (`--copy-auth`, `--host-home`, `--allow-unisolated-host-home`), `agent-relay` (`relay.sh`) execution, single-path `agent-xray` metering (streaming TTFT & OpenAI `/v1/responses`), Newcombe-Wilson 95% CI (`Inconclusive` / `看不出差别`), `--cli simulate` (replacing `--cli mock`), clean-checkout anti-cheat grading, detached-process leak detection, `pytest` preflight checks, and `./Scripts/test.sh`—live on the current development branch after `v0.2.0`. No real-agent leaderboard or screenshots are published yet pending a budget-approved live `SKILL_ARENA_REAL_AB=1` run.

## What SkillArena Does Today

1. **Per-Trial Isolated `HOME` & Workspace Sandbox** *(Unreleased after `v0.2.0`)* - Every `(task, contender, repetition)` runs in its own sandbox checkout and its own freshly created temporary `HOME` (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `VIBE_HOME`), cleaned up when the trial ends. **Control** sees `0` global skills and `0` workspace skills (pre-existing repo skill folders are stripped); **Treatment** sees **only** the candidate `--skill` mounted for that run, leaving the host `~/.claude` and `~/.agents/skills` untouched.
2. **Unified Real-Agent Execution via `agent-relay` (`relay.sh`)** *(Unreleased after `v0.2.0`)* - Routes headless execution for `claude`, `codex`, `agy`, and `vibe` exclusively through `agent-relay` (`relay.sh <vendor> <workdir> <prompt-file>`), delegating per-vendor flags, `RELAY_TIMEOUT`, and `RELAY_BUDGET_USD` to `relay.sh`, parsing `.relay/LOG.md` step status/cost/tokens, and cleaning `.relay/` before diff grading.
3. **Single-Path Streaming Metering via `agent-xray` (`Unmetered` When Unavailable)** *(Unreleased after `v0.2.0`)* - Delegates all HTTP/SSE wire metering to `agent-xray`'s `start_background_proxy(...)`, capturing streaming first-byte `ttft_ms`, Anthropic `/v1/messages`, OpenAI `/v1/chat/completions`, and OpenAI `/v1/responses` (`input_tokens`, `cached_tokens`, `output_tokens`, both SSE and JSON). When an agent does not traverse the Anthropic/OpenAI proxy (`agy`, `vibe`) or upstream omits `usage`, SkillArena reports `Unmetered` (`未计量`) rather than `0` tokens or `$0.0000`.
4. **Statistical Uncertainty (`Newcombe-Wilson 95% CI`) & Economics Metrics** *(Unreleased after `v0.2.0`)* - Computes a closed-form 95% Newcombe-Wilson confidence interval on `Pass@1` difference (`p_treatment - p_control`). Whenever sample size is below `5` trials per arm or the 95% CI spans `0`, SkillArena reports **`Inconclusive` (`看不出差别`)** and assigns **no winner rank**, alongside `Pass^k`, amortized `Cost-per-Success ($)`, **Win-Rate Derived Elo (2-arm)**, and **Turn-1 Context Tax** (*Mean Turn-1 input token delta: treatment minus baseline*).
5. **Clean-Checkout & SHA-256 Anti-Cheat Grader** *(Hardened, Unreleased after `v0.2.0`)* - Verifies byte-exact SHA-256 digests of protected test files, blocks agent-created `tests/*`, `__init__.py`, and stdlib-shadowing modules (`unittest.py`, `pytest.py`, `sitecustomize.py`), rejects `0`-test runs, fails trials on timeout or leaked background processes, and grades inside a clean baseline checkout carrying only permitted implementation edits.
6. **Git Commit Task Extractor (`extract-commit`)** *(Hardened, Unreleased after `v0.2.0`)* - Extracts a benchmark `TaskSpec` from a historical bug-fix commit: strips commit messages from the prompt, exports a standalone pre-fix tree snapshot without fix-commit Git history, and auto-detects `pytest` vs `unittest` (requiring `--test-cmd` when ambiguous).
7. **Bilingual CLI & Live Web Switcher** - Supports `--lang {auto,zh,en}`, `SKILL_ARENA_LANG`, and `LC_ALL` / `LANG` (`zh*`) auto-detection across CLI tables, Studio, and single-file HTML exports (`[SIMULATED]` banners included whenever synthetic mode is used).

## Current Limitations

- **Unverified live vendor login inside isolated `HOME` (`--copy-auth`)**: While isolated `HOME` staging and optional minimal credential copying (`--copy-auth`: `.claude/.credentials.json`, `.codex/auth.json`, `.vibe/.env`) are implemented and verified with offline stubs + real `relay.sh`, live login behavior across real paid `claude`, `codex`, `agy`, and `vibe` CLIs inside a fresh `HOME` has **not yet been executed** (`没跑`, pending user-approved live CLI runs). Similarly, the full 8-task live real-agent A/B (`SKILL_ARENA_REAL_AB=1`) has **not yet been run** offline to avoid unapproved API spend.
- **Explicit consent required if opting out of `HOME` isolation (`--host-home`, `K2`)**: Per-trial isolated `HOME` is enabled by default. Passing `--host-home` disables `HOME` isolation and is refused unless paired with `--allow-unisolated-host-home`.
- **Vendor CLI & proxy scope (`K7`–`K9`)**: Real-CLI execution delegates to `agent-relay` (`claude`, `codex`, `agy`, `vibe`); standalone `opencode` CLI flags were removed. `agent-xray` meters Anthropic (`/v1/messages`) and OpenAI (`/v1/chat/completions`, `/v1/responses`) HTTP/SSE traffic; CLIs using non-OpenAI/Anthropic transports (such as `agy` or Mistral `vibe`) report `Unmetered` for wire tokens while recording `relay.sh` step metadata.
- **Detached macOS SIP platform-binary daemons (`K13`)**: SkillArena injects a unique `SKILL_ARENA_TRIAL_TOKEN` per trial and scans `ps eww` alongside inherited `flock` descriptors and workspace `cwd` checks to `SIGKILL` escaped non-SIP processes (`python`, `node`, Homebrew tools, etc.) and mark the trial failed with `leaked_processes` recorded. However, on macOS with System Integrity Protection (`SIP`) enabled, Apple platform binaries (such as `/bin/sleep` or `/bin/sh`) strip environment variables from `ps eww` for unprivileged callers; if a trial spawns a `SIP`-protected binary that performs double-fork + `setsid()` + closes file descriptors + `chdir("/")`, host-mode execution **does not guarantee** detecting or reaping that process (full containment requires VM isolation).
- **`extract-commit` implementation filenames & `repo_path`**: While `extract-commit` removes commit subjects/bodies and exports a standalone pre-fix snapshot, the generated prompt currently lists the modified implementation filenames from the commit diff, and `TaskSpec.repo_path` points to the source repository path on disk (which a host-process agent could read outside its worktree).
- **Simulator scope (`K3`, Unreleased after `v0.2.0`)**: `--cli simulate` uses deterministic synthetic outcomes and token formulas to verify the pipeline offline. Every output is marked `SIMULATED`, and real model names or `--skill` paths are rejected in `simulate` mode.

## Quick Start

Requires **Python 3.11+**, `git`, and sibling/installed `agent-xray` + `agent-relay` for real-CLI runs (grading `pytest`-based tasks requires `pytest` in the active `python3` environment). Note: `--cli simulate`, `--copy-auth`, `--budget-usd`, and `./Scripts/test.sh` below are **Unreleased (after `main` / `v0.2.0`)**.

```bash
# 1. Inspect the built-in 8-task calibration suite (English & Simplified Chinese)
uv run skill-arena --lang en list-suites
uv run skill-arena --lang zh list-suites

# 2. Run an offline synthetic pipeline check (--cli simulate marks all outputs as SIMULATED)
uv run skill-arena run \
  --lang en \
  --cli simulate \
  --suite concurrency \
  --reps 1 \
  --db .skill-arena/sim-check.sqlite \
  --html-out .skill-arena/sim-check.html

uv run skill-arena compare --lang en --db .skill-arena/sim-check.sqlite
uv run skill-arena export-html --lang en --db .skill-arena/sim-check.sqlite --out .skill-arena/sim-export.html
```

### Running a Real Headless Coding Agent via `agent-relay` & Isolated `HOME` (`Unreleased after v0.2.0`)

```bash
# Runs each trial in an isolated temporary HOME via agent-relay (`relay.sh`) and agent-xray:
uv run skill-arena run \
  --lang en \
  --suite concurrency \
  --cli claude \
  --model claude-sonnet-4-5 \
  --skill /path/to/candidate-skill \
  --reps 3 \
  --budget-usd 2.0 \
  --copy-auth

# Launch local Web Studio (Scoreboard + Dual-Track Trajectory Replay)
uv run skill-arena studio --lang en --db .skill-arena/arena.sqlite --port 8765
```

## Extracting Benchmark Tasks from Git History

```bash
uv run skill-arena extract-commit \
  --lang en \
  --repo /path/to/your/repo \
  --commit HEAD \
  --out tasks/my-regression.json
```

## Built-In 8-Task Calibration Suite

| Task ID | Domain | Engineering Invariant Tested |
|---|---|---|
| `conc-01-token-bucket-race` | `concurrency` | 16-thread `TokenBucket` atomic refill & oversell prevention |
| `conc-02-async-flight-coalescer` | `concurrency` | Async `SingleFlight` request coalescing & error recovery |
| `ui-01-billing-ledger-slop` | `frontend_craft` | Refactor AI-slop HTML to 4px spacing grid & zero Hallmark warnings |
| `ui-02-incident-status-board` | `frontend_craft` | Semantic regional availability table with `tabular-nums` & craft gate |
| `refac-01-cron-expression-parser` | `refactor_test` | Cron range/step/list expansion & strict bounds validation |
| `refac-02-jsonl-ring-compactor` | `refactor_test` | Corrupt-tolerant JSONL ring compaction under dual record & byte caps |
| `sec-01-dsn-credential-redactor` | `security_hygiene` | Redaction of PostgreSQL DSNs, `Bearer` tokens, and `sk-*` keys |
| `sec-02-zip-slip-tar-extractor` | `security_hygiene` | Prevention of `../` Zip-Slip traversal and symlink escape in tarballs |

## Development & Scoped Verification (`Unreleased after v0.2.0`)

```bash
./Scripts/test.sh scope graders
./Scripts/test.sh scope sandbox
./Scripts/test.sh scope proxy
./Scripts/test.sh scope extractor
./Scripts/test.sh scope store
./Scripts/test.sh scope cli
./Scripts/test.sh milestone 0
./Scripts/test.sh milestone 1
```

## License

MIT License. See [LICENSE](LICENSE).
