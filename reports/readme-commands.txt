================================================================
SkillArena README.md & README.zh-CN.md Parsed Command Replay (W3)
================================================================

[Parity] Verified README.md and README.zh-CN.md list the same 16 CLI commands.

--- Replaying README.md (16 commands) ---
  RUN OK [README.md #1/16]: uv run skill-arena --lang en list-suites -> exit 0, verified suite listing
  RUN OK [README.md #2/16]: uv run skill-arena --lang zh list-suites -> exit 0, verified suite listing
  RUN OK [README.md #3/16]: uv run skill-arena run --lang en --cli simulate --suite concurrency --reps 1 --db .skill-arena/sim-check.sqlite --html-out .skill-arena/sim-check.html -> exit 0, verified SIMULATED CLI/SQLite/HTML
  RUN OK [README.md #4/16]: uv run skill-arena compare --lang en --db .skill-arena/sim-check.sqlite -> exit 0, verified leaderboard output
  RUN OK [README.md #5/16]: uv run skill-arena export-html --lang en --db .skill-arena/sim-check.sqlite --out .skill-arena/sim-export.html -> exit 0, verified exported HTML
  SKIPPED: uv run skill-arena run --lang en --suite concurrency --cli claude --model claude-sonnet-4-5 --skill /path/to/candidate-skill --reps 3 --budget-usd 2.0 --copy-auth — invokes real paid external agent CLI (claude) with placeholder /path/to/candidate-skill; prohibited in offline acceptance
  SKIPPED: uv run skill-arena studio --lang en --db .skill-arena/arena.sqlite --port 8765 — long-running blocking HTTP daemon on fixed port 8765 (probed on ephemeral port against product-generated sim-check.sqlite)
  RUN OK [README.md #8/16]: uv run skill-arena extract-commit --lang en --repo /path/to/your/repo --commit HEAD --out tasks/my-regression.json -> exit 0, verified extracted TaskSpec JSON
  RUN OK [README.md #9/16]: ./Scripts/test.sh scope graders -> exit 0, verified pytest passed
  RUN OK [README.md #10/16]: ./Scripts/test.sh scope sandbox -> exit 0, verified pytest passed
  RUN OK [README.md #11/16]: ./Scripts/test.sh scope proxy -> exit 0, verified pytest passed
  RUN OK [README.md #12/16]: ./Scripts/test.sh scope extractor -> exit 0, verified pytest passed
  RUN OK [README.md #13/16]: ./Scripts/test.sh scope store -> exit 0, verified pytest passed
  RUN OK [README.md #14/16]: ./Scripts/test.sh scope cli -> exit 0, verified pytest passed
  SKIPPED: ./Scripts/test.sh milestone 0 — top-level milestone orchestrator that invokes readme-commands.sh itself (skipped to prevent recursive self-invocation)
  SKIPPED: ./Scripts/test.sh milestone 1 — top-level milestone orchestrator that invokes readme-commands.sh itself (skipped to prevent recursive self-invocation)

--- Replaying README.zh-CN.md (16 commands) ---
  RUN OK [README.zh-CN.md #1/16]: uv run skill-arena --lang zh list-suites -> exit 0, verified suite listing
  RUN OK [README.zh-CN.md #2/16]: uv run skill-arena --lang en list-suites -> exit 0, verified suite listing
  RUN OK [README.zh-CN.md #3/16]: uv run skill-arena run --lang zh --cli simulate --suite concurrency --reps 1 --db .skill-arena/sim-check.sqlite --html-out .skill-arena/sim-check.html -> exit 0, verified SIMULATED CLI/SQLite/HTML
  RUN OK [README.zh-CN.md #4/16]: uv run skill-arena compare --lang zh --db .skill-arena/sim-check.sqlite -> exit 0, verified leaderboard output
  RUN OK [README.zh-CN.md #5/16]: uv run skill-arena export-html --lang zh --db .skill-arena/sim-check.sqlite --out .skill-arena/sim-export.html -> exit 0, verified exported HTML
  SKIPPED: uv run skill-arena run --lang zh --suite concurrency --cli claude --model claude-sonnet-4-5 --skill /path/to/candidate-skill --reps 3 --budget-usd 2.0 --copy-auth — invokes real paid external agent CLI (claude) with placeholder /path/to/candidate-skill; prohibited in offline acceptance
  SKIPPED: uv run skill-arena studio --lang zh --db .skill-arena/arena.sqlite --port 8765 — long-running blocking HTTP daemon on fixed port 8765 (probed on ephemeral port against product-generated sim-check.sqlite)
  RUN OK [README.zh-CN.md #8/16]: uv run skill-arena extract-commit --lang zh --repo /path/to/your/repo --commit HEAD --out tasks/my-regression.json -> exit 0, verified extracted TaskSpec JSON
  SKIPPED: ./Scripts/test.sh scope graders — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh scope sandbox — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh scope proxy — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh scope extractor — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh scope store — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh scope cli — byte-identical scoped test command already executed and verified in README.md pass
  SKIPPED: ./Scripts/test.sh milestone 0 — top-level milestone orchestrator that invokes readme-commands.sh itself (skipped to prevent recursive self-invocation)
  SKIPPED: ./Scripts/test.sh milestone 1 — top-level milestone orchestrator that invokes readme-commands.sh itself (skipped to prevent recursive self-invocation)

================================================================
RESULT: README COMMAND REPLAY SUMMARY: 18 passed, 14 skipped, 0 failed (out of 32 parsed commands)
================================================================
