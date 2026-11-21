# SkillArena

**English** | **[简体中文](README.zh-CN.md)**

**Controlled Git-worktree & isolated-`HOME` A/B benchmark harness** for coding agents and `SKILL.md` rules, featuring per-trial temporary `HOME` isolation, unified headless agent execution via `agent-relay` (`relay.sh`), streaming HTTP/SSE & OpenAI Responses token metering via `agent-xray`, Newcombe-Wilson 95% confidence intervals (`Inconclusive` / `看不出差别` when noisy), clean-checkout deterministic unit & Hallmark craft grading, SHA-256 anti-cheat verification, and a bilingual (`中文 | EN`) dual-track trajectory replay Studio.

> **Release status**: Latest tagged release is `v0.4.0`. Capabilities marked **since `v0.3.0`** below—including per-trial isolated `HOME` (`--pass-env`, `--copy-auth`, `--host-home`, `--allow-unisolated-host-home`), `agent-relay` (`relay.sh`) execution, single-path `agent-xray` metering (streaming TTFT & OpenAI `/v1/responses`), Newcombe-Wilson 95% CI (`Inconclusive` / `看不出差别`), `--cli simulate` (replacing `--cli mock`), clean-checkout anti-cheat grading, detached-process leak detection, `pytest` preflight checks, and `./Scripts/test.sh`—first released in `v0.3.0` (not present in `v0.2.0`); capabilities marked **since `v0.4.0`**—task-paired 95% CI & invalid-run check, agent-authored `test_*.py` allowance, `pool-export`, multi-arm `--arm` / `--seed` / `--workers` experiment design, and `skill-arena verify`—first released in `v0.4.0`. No real-agent leaderboard or screenshots are published yet pending a budget-approved live `SKILL_ARENA_REAL_AB=1` run.

## What SkillArena Does Today

1. **Per-Trial Isolated `HOME` & Strict Child-Env Allowlist** *(since `v0.3.0`)* - Every `(task, contender, repetition)` runs in its own sandbox checkout with a strict system allowlist of child environment variables (forwarding **zero** host provider API key environment variables by default unless explicitly named via `--pass-env <VAR>`, stripping `SSH_AUTH_SOCK`, `CLAUDE_CODE_*`, and parent session tokens, and copying `0` CLI credential files unless `--copy-auth` is requested) plus its own freshly created temporary `HOME` and `TMPDIR` (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `VIBE_HOME`), cleaned up when the trial ends. **Control** sees `0` global skills and `0` workspace skills (pre-existing repo skill folders are stripped); **Treatment** sees **only** the candidate `--skill` mounted for that run via environment-configured paths (note: host-mode isolation is environment-variable and worktree level only; see *Current Limitations*).
2. **Unified Real-Agent Execution via `agent-relay` (`relay.sh`) & Experiment-Wide `--budget-usd` Ceiling** *(since `v0.3.0`)* - Routes headless execution for `claude`, `codex`, `agy`, and `vibe` exclusively through `agent-relay` (`relay.sh <vendor> <workdir> <prompt-file>`), delegating per-vendor flags and `RELAY_TIMEOUT` to `relay.sh`, parsing `.relay/LOG.md` step status/cost/tokens alongside `agent-xray` wire metering, and cleaning `.relay/` before diff grading. `--budget-usd` acts as an **experiment-wide cumulative ceiling** across the entire A/B run: SkillArena tracks cumulative spend across executed trials, passes remaining budget as `RELAY_BUDGET_USD`, halts before launching the next trial when another trial would exceed `--budget-usd`, and exits with dedicated non-zero exit code **`3`** (`EXIT_BUDGET_EXCEEDED = 3`) whenever `--budget-usd` halts a run or is exceeded (note: when `--budget-usd` is smaller than the cost of a single trial, trial 1 still executes before cumulative cost can be checked and can overrun `--budget-usd` on that first trial before halting; see *Current Limitations*).
3. **Single-Path Streaming Metering via `agent-xray` (`Unmetered` When Unavailable)** *(since `v0.3.0`)* - Delegates all HTTP/SSE wire metering to `agent-xray` (`start_background_proxy(...)`, pinned to `agent-xray` commit `3cd9b14dcbae54505c56f982d13fae862d606e5a` (agent-xray v0.3.0), emitting an explicit `agent-xray commit unverified` warning to `stderr` if loaded from a non-git copy without a resolvable commit SHA), capturing streaming first-byte `ttft_ms`, Anthropic `/v1/messages`, OpenAI `/v1/chat/completions`, and OpenAI `/v1/responses` (`input_tokens`, `cached_tokens`, `output_tokens`, both SSE and JSON). When an agent does not traverse the Anthropic/OpenAI proxy (`agy`, `vibe`) or upstream omits `usage`, SkillArena reports `Unmetered` (`未计量`) rather than `0` tokens or `$0.0000`.
4. **Statistical Uncertainty (task-paired 95% CI) & Economics Metrics** *(since `v0.3.0`; task pairing and the time-limit check since `v0.4.0`)* - Computes a 95% confidence interval on the `Pass@1` difference (`p_treatment - p_control`) with the **task as the unit of analysis** (Miller 2024, *Adding Error Bars to Evals*, arXiv 2411.00640): each task's `k` repeats are reduced to a pass rate first, `d_i = p_T,i - p_C,i`, and the standard error is the larger of the between-task estimate `sqrt(sum((d_i - d_bar)^2) / (n(n-1)))` and an Agresti-Caffo within-task estimate, with a Student-t critical value on `n - 1` degrees of freedom. The report also gives the **minimum detectable effect** at 80% power, so a small suite says up front how large a difference it could have seen. Whenever sample size is below `5` trials per arm or the 95% CI spans `0`, SkillArena reports **`Inconclusive` (`看不出差别`)** and assigns **no winner rank**. If more than **10%** of either arm's trials were stopped by the time limit, the run is reported as **`Invalid run` (`实验无效`)** with no winner: a skill that spends its first minute planning or writing tests otherwise looks worse only because it was cut off. The default per-task `timeout_sec` is **900 s** (it was 90 s; agents need about 2 minutes on the built-in tasks). Alongside: `Pass^k`, amortized `Cost-per-Success ($)`, **Win-Rate Derived Elo (2-arm)**, and **Turn-1 Context Tax** (*Mean Turn-1 input token delta: treatment minus baseline*).
5. **Clean-Checkout & SHA-256 Anti-Cheat Grader** *(Hardened, since `v0.3.0`)* - Verifies byte-exact SHA-256 digests of protected test files, blocks agent edits or deletions under `tests/`, agent-created `tests/` helpers, `conftest.py`, `__init__.py`, and stdlib-shadowing modules (`unittest.py`, `pytest.py`, `sitecustomize.py`); since `v0.4.0`, an agent may *add* new `tests/**/test_*.py` modules of its own (for example under a test-driven-development skill) without an anti-cheat violation, and those files are never copied into the grading checkout, rejects `0`-test runs, fails trials on timeout or leaked background processes, and grades inside a clean baseline checkout carrying only permitted implementation edits.
6. **Git Commit Task Extractor (`extract-commit`)** *(Hardened, since `v0.3.0`)* - Extracts a benchmark `TaskSpec` from a historical bug-fix commit: strips commit messages from the prompt, exports a standalone pre-fix tree snapshot without fix-commit Git history, and auto-detects `pytest` vs `unittest` (requiring `--test-cmd` when ambiguous).
7. **Bilingual CLI & Live Web Switcher** - Supports `--lang {auto,zh,en}`, `SKILL_ARENA_LANG`, and `LC_ALL` / `LANG` (`zh*`) auto-detection across CLI tables, Studio, and single-file HTML exports (`[SIMULATED]` banners included whenever synthetic mode is used).
8. **Experiment Design for Large A/B Runs** *(since `v0.4.0`)* - `pool-export` turns a task pool (`task.json`, `start/`, `tests/`, `reference/`, `mutants/<name>/`) into TaskSpec JSON in a **visible** variant (tests in the workspace, protected) and a **hidden** variant (no tests in the workspace; the grader overlays them onto the clean grading checkout, so an agent file at a hidden-test path is overwritten, not counted as tampering); ids get `@visible` / `@hidden`. `--arm LABEL=PATH[,PATH...]` (repeatable) compares several skills against the always-present no-skill control: each comparison gets the task-paired CI, an exact sign-flip permutation p-value on the per-task differences (seeded Monte Carlo with 20000 draws beyond 16 non-zero tasks) and a Holm-adjusted p-value. Trials run in a randomized blocked order (each `(task, rep)` block holds every arm; `--seed`) on `--workers N` threads, with the launch index and start/finish time recorded. Every verdict carries a failure category (`passed`, `wrong`, `interrupted`, `anti_cheat`, `craft`, `infra`, `grader_error`) and the complete grader output (up to 200 KB); the Hallmark `sloplint.mjs` is vendored (MIT, see `graders/vendor/hallmark/NOTICE.txt`; `SKILL_ARENA_SLOPLINT` overrides it) and the verdict names the copy used. Reports add, per arm, a time-limit curve `Pass@T` (passed **and** finished within T = 60/120/180/300/600/900 s and the limit), the mutation score of the tests the agent wrote itself (run against the reference and each mutant), behaviour metrics (test runs, test-first, whether `SKILL.md` was read; only Mistral `vibe` records tool calls through `agent-relay`, other CLIs show n/a), and per-category (and per-variant) task-paired deltas.
9. **Deterministic Repository & Self-Report Verifier (`skill-arena verify`)** *(since `v0.4.0`)* - Clones a target Git repository via `git clone --no-local` into a single temporary root (`repo/`, `home/`, `tmp/`, `bin/`), wraps every child process in macOS `sandbox-exec` (blocking outbound internet and real-`HOME` writes except shared `~/.cache/uv`), runs auto-detected build and baseline tests (`0` tests executed counts as failure), replays root-level `README` commands verbatim, runs deterministic single-point AST/Swift destructive mutations to confirm tests actually fail on broken code, and audits milestone self-reports (`--self-report`) for unsupported absolute claims; two runs on the same commit yield byte-identical JSON once `timing_meta` is stripped.

## Current Limitations

- **Unverified live vendor login inside isolated `HOME` (`--copy-auth`)**: While isolated `HOME` staging and optional minimal credential copying (`--copy-auth`: `.claude/.credentials.json`, `.codex/auth.json`, `.vibe/.env`) are implemented and verified with offline stubs + real `relay.sh`, live login behavior across real paid `claude`, `codex`, `agy`, and `vibe` CLIs inside a fresh `HOME` has **not yet been executed** (`没跑`, pending user-approved live CLI runs). Similarly, the full 8-task live real-agent A/B (`SKILL_ARENA_REAL_AB=1`) has **not yet been run** offline to avoid unapproved API spend.
- **Host-mode isolation covers environment variables & the workspace worktree only (`KR4`, `KR6`)**: By default, SkillArena builds each child agent's environment from a strict system allowlist (`HOME`, `TMPDIR`, `XDG_*`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `VIBE_HOME`, stripping `SSH_AUTH_SOCK`, `CLAUDE_CODE_*`, and session tokens), forwards **zero** host provider API key environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MISTRAL_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, etc.) unless the user explicitly names each variable with `--pass-env <VAR>`, and copies **zero** host CLI credential files into the per-trial isolated `HOME` unless `--copy-auth` is explicitly passed. However, host-mode execution does **not** create an OS user or filesystem namespace: the tested agent process runs as the same OS user, `pwd.getpwuid(os.getuid()).pw_dir` still resolves to the real account home directory, and the agent process can read or write paths outside the worktree. Full OS and filesystem isolation requires running inside `AgentVM`.
- **Budget-halt exit code (`3`) & single-trial overrun boundary (`KR2`, `KR8`)**: When `--budget-usd` halts a run before completing all planned trials (or when cumulative spend exceeds `--budget-usd`), `skill-arena run` records completed trials, renders the comparison summary, and exits with dedicated non-zero exit code **`3`** (`EXIT_BUDGET_EXCEEDED = 3`). Because cumulative spend can only be checked between trials, when `--budget-usd` is smaller than the cost of a single trial, the first trial still executes before cumulative cost can be checked—so total spend can exceed `--budget-usd` on trial `1` before SkillArena halts prior to trial `2` and exits `3`.
- **Explicit consent required if opting out of `HOME` isolation (`--host-home`, `K2`)**: Per-trial isolated `HOME` is enabled by default. Passing `--host-home` disables `HOME` isolation and is refused unless paired with `--allow-unisolated-host-home`.
- **Vendor CLI & proxy scope (`K7`–`K9`)**: Real-CLI execution delegates to `agent-relay` (`claude`, `codex`, `agy`, `vibe`); standalone `opencode` CLI flags were removed. `agent-xray` meters Anthropic (`/v1/messages`) and OpenAI (`/v1/chat/completions`, `/v1/responses`) HTTP/SSE traffic; CLIs using non-OpenAI/Anthropic transports (such as `agy` or Mistral `vibe`) report `Unmetered` for wire tokens while recording `relay.sh` step metadata.
- **Detached macOS SIP platform binaries & environment-scrubbing daemons (`K13`, `K16`)**: SkillArena injects a unique `SKILL_ARENA_TRIAL_TOKEN` per trial and combines inherited `flock` descriptors, trial-sandbox `cwd` and open-file (`lsof +D`) scans, and `ps eww` inspection to `SIGKILL` escaped processes and mark the trial failed with `leaked_processes` recorded. However, because macOS lacks unprivileged PID namespaces/cgroups and diffing all host-UID processes would risk signaling unrelated concurrent user tasks, host-mode execution **does not guarantee** detecting or reaping detached (`setsid` / double-fork) processes that leave the sandbox directory (`chdir`), close all workspace and lock file descriptors, **and** either (a) execute a macOS `SIP`-protected Apple platform binary (such as `/bin/sleep` or `/bin/sh`, which strips environment variables from `ps eww` for unprivileged callers) or (b) scrub `SKILL_ARENA_TRIAL_TOKEN` from their environment (e.g. `execve(..., env={})`, `os.environ.pop`, or `env -i`). Full containment of these two classes requires VM isolation.
- **`extract-commit` implementation filenames & `repo_path`**: While `extract-commit` removes commit subjects/bodies and exports a standalone pre-fix snapshot, the generated prompt currently lists the modified implementation filenames from the commit diff, and `TaskSpec.repo_path` points to the source repository path on disk (which a host-process agent could read outside its worktree).
- **Repeated runs are not independent samples (`KR5`)**: Up to `v0.3.0` the interval was a Newcombe-Wilson CI that pooled all `tasks * k_reps` trial outcomes per arm and treated repeated runs (`--reps > 1`) of the same task as independent Bernoulli trials, which made it narrower than it should be. It is now computed per task as described above. With few tasks the interval is wide on purpose: 8 tasks x 3 reps can only detect differences of roughly 30 percentage points; adding tasks narrows it far more than adding `--reps`.
- **Parallel workers and the budget**: with `--workers N` the first `N` trials start before any cost is known, so a run can overshoot `--budget-usd` by up to `N` trials; after that each check reserves the cost of the trials still running. `vibe` gets `--time-limit` from `agent-relay`; it is a client-side interrupt that is not shown to the model (vibe 3.1.0 prompts and session journals do not contain it).
- **Simulator scope (`K3`, `KR5`, since `v0.3.0`)**: `--cli simulate` uses deterministic synthetic outcomes and token formulas to verify the pipeline offline. Every output—including the final statistical verdict line—is marked `[SIMULATED]`, and real model names or `--skill` paths are rejected in `simulate` mode.

## Quick Start

Requires **Python 3.11+**, `git`, and sibling/installed `agent-xray` + `agent-relay` for real-CLI runs (grading `pytest`-based tasks requires `pytest` in the active `python3` environment). Note: `--cli simulate`, `--pass-env`, `--copy-auth`, `--budget-usd`, and `./Scripts/test.sh` below are **since `v0.3.0`**.

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

### Running a Real Headless Coding Agent via `agent-relay` & Isolated `HOME` (since v0.3.0)

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

## Running a Designed Experiment from a Task Pool

```bash
# Export both variants of every task in a pool (see the pool's FORMAT.md)
uv run skill-arena pool-export \
  --pool /path/to/pool \
  --variant visible \
  --variant hidden \
  --timeout-sec 900 \
  --out suites/pool-ab.json

# Two skills against the no-skill control, randomized blocked order, 4 trials at a time
uv run skill-arena run \
  --lang en \
  --suite suites/pool-ab.json \
  --cli vibe \
  --model deepseek-v4-1-flash-1017 \
  --arm tdd=/path/to/tdd-skill \
  --arm review=/path/to/review-skill \
  --reps 3 \
  --seed 20261118 \
  --workers 4 \
  --budget-usd 30 \
  --html-out .skill-arena/pool-ab.html
```

## Extracting Benchmark Tasks from Git History

```bash
uv run skill-arena extract-commit \
  --lang en \
  --repo /path/to/your/repo \
  --commit HEAD \
  --out tasks/my-regression.json
```

## Repository Verifier (`skill-arena verify`)

`skill-arena verify` verifies a Git repository and/or a milestone self-report deterministically without trusting author narratives:

- **Command & Parameters**:
  `uv run skill-arena verify [--lang {auto,zh,en}] [--repo <path>] [--ref <sha|tag|branch>] [--build-cmd <cmd>] [--test-cmd <cmd>] [--mutate | --no-mutate] [--max-mutations <N>] [--self-report <md>] [--skip-cmd <pattern>] [--timeout-sec <sec>] [--md-out <path>] [--json-out <path>]`. Passing `--self-report` without `--repo` audits the Markdown report standalone.
- **Three Repository Checks & Self-Report Wording Audit**:
  1. **Clean-Clone Build & Baseline Tests**: Stages `repo/`, `home/`, `tmp/`, and `bin/` inside a single `mkdtemp` root via `git clone --no-local`, disables push to `origin`, compares read-only fingerprints (`HEAD`, `.git/index`, `git status --porcelain=v1 --ignored=matching`, `show-ref`) of the source repository before and after execution, auto-detects build/test commands from project files (`uv.lock` -> `uv sync --offline --frozen` + `uv run pytest`; `Package.swift` -> `swift build --disable-sandbox` + `swift test --disable-sandbox`), rejects `0`-test runs, cleans read-only files in `finally`, and reaps leaked background processes.
  2. **Root `README` Command Replay**: Scans only root-level `README*.md` files (never recursing into subdirectory fixtures), verifies bilingual `README.md` / `README.zh-CN.md` command parity, executes offline-verifiable shell commands verbatim, classifies network/API-key/interactive/daemon/Docker/placeholder/user-home/release commands as `UNVERIFIABLE` with explicit reasons, and marks unstated-precondition failures as `FAIL` (unless explicitly exempted via `--skip-cmd`, disclosed in the report).
  3. **Destructive Single-Point Mutation Check**: Generates deterministic `(path, line, col)`-ordered single-point mutations on non-test source files (Python `ast` operators and pre-built Swift line operators; default `1`, up to `--max-mutations`), counting only genuine test assertion failures as `KILLED` (syntax, import, collection, and compile errors are `INVALID`, never counted as killed). Surviving mutations fail verification; original bytes are restored and verified clean (`git status --porcelain` empty).
  4. **Self-Report Wording Audit (`--self-report`)**: Scans the 10-term absolute-claim vocabulary (`彻底`, `全部`, `零`, `已修`, `绝不`, `无任何`, `100%`, `never`, `zero`, `completely`), requiring same-item runnable command + output/exit-code evidence (`SUPPORTED`) and flagging unsupported claims as `UNSUPPORTED` (exit `1`), with 4 deterministic denoising rules (`非零` / `non-zero`, headings & `:` table/list lead-ins, `不在范围` / `没验证` scope disclaimers, and quoted deleted/retracted legacy copy).
- **Offline & `HOME` Isolation Rules (Including the `~/.cache/uv` Exception)**:
  Every child subprocess runs under macOS `/usr/bin/sandbox-exec` with an allowlisted offline environment (`HOME`, `TMPDIR`, and `XDG_*` inside the temporary workspace, `UV_OFFLINE=1`, `PIP_NO_INDEX=1`, HTTP/HTTPS Git transports disabled). Outbound internet connections are blocked by Seatbelt (`deny network*`, allowing only `localhost` loopback and Unix sockets). Writes to the real account `HOME` (`pwd.getpwuid(os.getuid()).pw_dir`, including `~/Library`) are blocked by `sandbox-exec` **except `<USER_HOME>/.cache/uv`**, which is shared so offline `uv sync` avoids cloning gigabytes of wheel caches per run; newly added entries under `~/.cache/uv` are counted and reported in `timing_meta.uv_cache_new_entries`.
- **Normalization Before Deterministic Comparison**:
  Volatile wall-clock durations and `~/.cache/uv` entry deltas are isolated under top-level `timing_meta`. Across all other report fields, `PathScrubber` and `summarize_command_output` strip ANSI escape sequences, scrub temporary, repository, and user paths (`<TMP_REPO>`, `<TMP_HOME>`, `<TMPDIR>`, `<TMP_ROOT>`, `<REPO_ROOT>`, `<USER_HOME>`), replace inline tool timing durations (`in 9.87s`, `in 102.31s (0:01:42)`, `in 9.87 seconds`, `Ran N tests in 0.003s`, `finished in 0.01s`, `after 0.004 seconds`, `in 0.01 (0.02) seconds`) with `<TIME>`, and normalize `pytest`-style `=`-padded summary banners to a fixed three-equals form (`=== <summary> in <TIME> ===`) prior to summary length truncation—leaving test names, dot-progress lines, section headers, and failure messages untouched.
- **Exit Codes**:
  `0` when all enabled verification checks pass; `1` when any verification check fails (build/test failure, `0` tests executed, unstated-precondition `README` command failure, surviving mutation, leaked process, or unsupported wording); `2` on invalid arguments, missing self-report file, non-Git target directory, or source repository fingerprint mismatch.

```bash
# 1. Verify a Git repository in an isolated offline clone (build, tests, root README replay, and mutation check)
uv run skill-arena verify \
  --lang en \
  --repo /path/to/your/repo \
  --md-out .skill-arena/verify-report.md \
  --json-out .skill-arena/verify-report.json

# 2. Audit a milestone self-report offline for unsupported absolute claims (bundled sanitized sample)
uv run skill-arena verify \
  --lang en \
  --self-report tests/fixtures/verify_sample_self_report.md \
  --json-out .skill-arena/self-report-audit.json
```

### Measured `diy-harness` Verification Summary (2026-11-21)

Measured via `./Scripts/acceptance/m2-acceptance.sh` (`reports/m2-acceptance.txt`, 2026-11-21) running `uv run skill-arena --lang zh verify --repo <USER_HOME>/Projects/diy-harness` twice consecutively against commit `52207fd803d4`:

- **Exit code & determinism**: Both runs exited `1` (`FAIL`) and produced byte-identical JSON (`36,031` bytes after stripping `timing_meta`, `sha256=db7a3bd1ad5a6420...`), with source fingerprint unchanged, temporary clone deleted, and `0` leaked processes.
- **Build & baseline tests**: `uv sync --offline --frozen` exited `0` (**PASS**); `uv run pytest` exited `0` (**PASS**, `1154 passed, 4 skipped, 2 deselected`).
- **Root `README.md` replay**: `18` fenced blocks (`11` shell blocks, `7` non-command blocks) -> `58` commands total: **`11` passed**, **`2` failed** (`uv run python scripts/sync_prompts.py --check` exit `2` and `bash scripts/ci_sim.sh` exit `1`, both due to unstated external `~/deepagents` / network preconditions), **`45` unverifiable** (including `uv tool install --editable .` classified as offline-unverifiable).
- **Destructive mutation**: **PASS** — candidate #1 `src/diy_harness/acp.py:L214` (`compare_negate`: `<=` -> `>`) **KILLED** by `uv run pytest tests/unit_tests/test_acp.py -q` (exit `1`, `3 failed, 57 passed`), restored with tests passing and clean worktree.

```text
[verify] diy-harness @ 52207fd803d4 -> FAIL (build=PASS, test=PASS, readme=11 pass / 2 fail / 45 unverifiable, mutate=PASS)
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

## Development & Scoped Verification (since v0.3.0)

```bash
./Scripts/test.sh scope graders
./Scripts/test.sh scope sandbox
./Scripts/test.sh scope proxy
./Scripts/test.sh scope extractor
./Scripts/test.sh scope store
./Scripts/test.sh scope cli
./Scripts/test.sh scope verifier
./Scripts/test.sh milestone 0
./Scripts/test.sh milestone 1
./Scripts/test.sh milestone 2
```

## License

MIT License. See [LICENSE](LICENSE).
