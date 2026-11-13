"""Official 8-task SkillArena benchmark suite across Concurrency, Frontend Craft, Refactor, and Security."""

from __future__ import annotations

from skill_arena.models import TaskSpec


def load_builtin_suite(category: str | None = None) -> list[TaskSpec]:
    tasks = [
        _task_conc_01_token_bucket(),
        _task_conc_02_singleflight(),
        _task_ui_01_billing_slop(),
        _task_ui_02_incident_board(),
        _task_refac_01_cron_parser(),
        _task_refac_02_jsonl_compactor(),
        _task_sec_01_dsn_redactor(),
        _task_sec_02_archive_slip(),
    ]
    if category:
        return [t for t in tasks if t.category == category]
    return tasks


def _task_conc_01_token_bucket() -> TaskSpec:
    return TaskSpec(
        task_id="conc-01-token-bucket-race",
        title="Thread-safe TokenBucket burst & monotonic refill under 16-thread contention",
        category="concurrency",
        prompt=(
            "Fix the thread-safety and clock-refill race in `rate_limiter.py`. "
            "Under 16 concurrent worker threads, `try_acquire()` currently oversells tokens beyond capacity "
            "because refill and deduction are not synchronized atomically. Do not modify `tests/test_rate_limiter.py`."
        ),
        test_command="python3 -m unittest tests/test_rate_limiter.py",
        protected_test_paths=["tests/test_rate_limiter.py"],
        seed_files={
            "rate_limiter.py": (
                "import time\n\n"
                "class TokenBucket:\n"
                "    def __init__(self, capacity: int, refill_per_sec: float) -> None:\n"
                "        self.capacity = float(capacity)\n"
                "        self.tokens = float(capacity)\n"
                "        self.refill_per_sec = float(refill_per_sec)\n"
                "        self.last_ts = time.monotonic()\n\n"
                "    def try_acquire(self, amount: int = 1) -> bool:\n"
                "        now = time.monotonic()\n"
                "        elapsed = max(0.0, now - self.last_ts)\n"
                "        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)\n"
                "        if self.tokens >= amount:\n"
                "            time.sleep(0.0002)\n"
                "            self.tokens -= amount\n"
                "            self.last_ts = now\n"
                "            return True\n"
                "        return False\n"
            ),
            "tests/test_rate_limiter.py": (
                "import threading\nimport unittest\nfrom rate_limiter import TokenBucket\n\n"
                "class TokenBucketTest(unittest.TestCase):\n"
                "    def test_no_oversell_under_contention(self) -> None:\n"
                "        bucket = TokenBucket(capacity=25, refill_per_sec=0.0)\n"
                "        grants = []\n"
                "        lock = threading.Lock()\n"
                "        def worker() -> None:\n"
                "            for _ in range(10):\n"
                "                if bucket.try_acquire(1):\n"
                "                    with lock:\n"
                "                        grants.append(1)\n"
                "        threads = [threading.Thread(target=worker) for _ in range(16)]\n"
                "        for t in threads: t.start()\n"
                "        for t in threads: t.join()\n"
                "        self.assertEqual(sum(grants), 25)\n"
                "        self.assertFalse(bucket.try_acquire(1))\n\n"
                "    def test_rejects_negative_or_excess_request(self) -> None:\n"
                "        bucket = TokenBucket(capacity=5, refill_per_sec=10.0)\n"
                "        self.assertFalse(bucket.try_acquire(6))\n"
                "        with self.assertRaises(ValueError):\n"
                "            bucket.try_acquire(0)\n"
            ),
        },
        naive_attempt_files={
            "rate_limiter.py": (
                "import threading\nimport time\n\n"
                "class TokenBucket:\n"
                "    def __init__(self, capacity: int, refill_per_sec: float) -> None:\n"
                "        self.capacity = float(capacity)\n"
                "        self.tokens = float(capacity)\n"
                "        self.refill_per_sec = float(refill_per_sec)\n"
                "        self.last_ts = time.monotonic()\n"
                "        self._lock = threading.Lock()\n\n"
                "    def try_acquire(self, amount: int = 1) -> bool:\n"
                "        with self._lock:\n"
                "            now = time.monotonic()\n"
                "            self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.last_ts) * self.refill_per_sec)\n"
                "            self.last_ts = now\n"
                "            if self.tokens >= amount:\n"
                "                self.tokens -= amount\n"
                "                return True\n"
                "            return False\n"
            )
        },
        reference_solution_files={
            "rate_limiter.py": (
                "import threading\nimport time\n\n"
                "class TokenBucket:\n"
                "    def __init__(self, capacity: int, refill_per_sec: float) -> None:\n"
                "        self.capacity = float(capacity)\n"
                "        self.tokens = float(capacity)\n"
                "        self.refill_per_sec = float(refill_per_sec)\n"
                "        self.last_ts = time.monotonic()\n"
                "        self._lock = threading.Lock()\n\n"
                "    def try_acquire(self, amount: int = 1) -> bool:\n"
                "        if amount <= 0:\n"
                "            raise ValueError('amount must be positive')\n"
                "        with self._lock:\n"
                "            now = time.monotonic()\n"
                "            elapsed = max(0.0, now - self.last_ts)\n"
                "            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)\n"
                "            self.last_ts = now\n"
                "            if self.tokens >= amount:\n"
                "                self.tokens -= amount\n"
                "                return True\n"
                "            return False\n"
            )
        },
        max_diff_lines=40,
    )


def _task_conc_02_singleflight() -> TaskSpec:
    return TaskSpec(
        task_id="conc-02-async-flight-coalescer",
        title="Async SingleFlight request coalescer without stuck keys on upstream failure",
        category="concurrency",
        prompt=(
            "Fix `coalescer.py` so concurrent `do(key, fn)` coroutines for the same key share a single upstream "
            "execution, propagate exceptions cleanly to all waiters, and always evict the in-flight entry afterwards "
            "so subsequent retries can succeed."
        ),
        test_command="python3 -m unittest tests/test_coalescer.py",
        protected_test_paths=["tests/test_coalescer.py"],
        seed_files={
            "coalescer.py": (
                "import asyncio\nfrom typing import Awaitable, Callable, TypeVar\n\n"
                "T = TypeVar('T')\n\n"
                "class SingleFlight:\n"
                "    def __init__(self) -> None:\n"
                "        self._inflight: dict[str, asyncio.Future] = {}\n\n"
                "    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:\n"
                "        if key in self._inflight:\n"
                "            return await self._inflight[key]\n"
                "        fut = asyncio.get_running_loop().create_future()\n"
                "        self._inflight[key] = fut\n"
                "        res = await fn()\n"
                "        fut.set_result(res)\n"
                "        del self._inflight[key]\n"
                "        return res\n"
            ),
            "tests/test_coalescer.py": (
                "import asyncio\nimport unittest\nfrom coalescer import SingleFlight\n\n"
                "class SingleFlightTest(unittest.TestCase):\n"
                "    def test_coalesces_and_recovers_after_error(self) -> None:\n"
                "        async def scenario() -> None:\n"
                "            sf = SingleFlight()\n"
                "            calls = 0\n"
                "            async def flaky() -> str:\n"
                "                nonlocal calls\n"
                "                calls += 1\n"
                "                await asyncio.sleep(0.02)\n"
                "                if calls == 1:\n"
                "                    raise RuntimeError('boom')\n"
                "                return 'ok'\n"
                "            results = await asyncio.wait_for(asyncio.gather(\n"
                "                sf.do('k1', flaky), sf.do('k1', flaky), return_exceptions=True\n"
                "            ), timeout=0.5)\n"
                "            self.assertEqual(calls, 1)\n"
                "            self.assertTrue(all(isinstance(r, RuntimeError) for r in results))\n"
                "            recovered = await sf.do('k1', flaky)\n"
                "            self.assertEqual(recovered, 'ok')\n"
                "            self.assertEqual(calls, 2)\n"
                "        asyncio.run(scenario())\n"
            ),
        },
        naive_attempt_files={
            "coalescer.py": (
                "import asyncio\nfrom typing import Awaitable, Callable, TypeVar\n\n"
                "T = TypeVar('T')\n\n"
                "class SingleFlight:\n"
                "    def __init__(self) -> None:\n"
                "        self._inflight: dict[str, asyncio.Future] = {}\n\n"
                "    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:\n"
                "        if key in self._inflight:\n"
                "            return await self._inflight[key]\n"
                "        fut = asyncio.get_running_loop().create_future()\n"
                "        self._inflight[key] = fut\n"
                "        try:\n"
                "            res = await fn()\n"
                "            fut.set_result(res)\n"
                "            return res\n"
                "        finally:\n"
                "            self._inflight.pop(key, None)\n"
            )
        },
        reference_solution_files={
            "coalescer.py": (
                "import asyncio\nfrom typing import Awaitable, Callable, TypeVar\n\n"
                "T = TypeVar('T')\n\n"
                "class SingleFlight:\n"
                "    def __init__(self) -> None:\n"
                "        self._tasks: dict[str, asyncio.Task] = {}\n\n"
                "    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:\n"
                "        task = self._tasks.get(key)\n"
                "        is_owner = False\n"
                "        if task is None:\n"
                "            task = asyncio.create_task(fn())\n"
                "            self._tasks[key] = task\n"
                "            is_owner = True\n"
                "        try:\n"
                "            return await asyncio.shield(task)\n"
                "        finally:\n"
                "            if is_owner:\n"
                "                self._tasks.pop(key, None)\n"
            )
        },
        max_diff_lines=45,
    )


def _task_ui_01_billing_slop() -> TaskSpec:
    return TaskSpec(
        task_id="ui-01-billing-ledger-slop",
        title="Refactor AI-slop SaaS billing ledger HTML to strict 4px grid & Hallmark compliance",
        category="frontend_craft",
        prompt=(
            "Refactor `ui/billing.html` so it passes both `tests/test_billing_dom.py` and strict Hallmark craft checks: "
            "remove generic `#0b0e14` navy slop, eliminate em-dashes in copy, remove nested `<span>` inside `<h1>`, "
            "enforce a strict 4px spacing scale, add `<meta name=\"description\">`, and set `text-wrap: balance` on headings."
        ),
        test_command="python3 -m unittest tests/test_billing_dom.py",
        protected_test_paths=["tests/test_billing_dom.py"],
        frontend_target_html="ui/billing.html",
        seed_files={
            "ui/billing.html": (
                "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<title>NovaBilling — Enterprise Usage</title>"
                "<style>body{background:#0b0e14;color:#e2e8f0;padding:14px;margin:0;}"
                ".card{padding:18px;margin-bottom:10px;}</style></head>"
                "<body><h1>Usage Ledger <span style='color:#64748b'>Pro Plan</span></h1>"
                "<div class='card' id='invoice-total'>$1,420.00 — Current cycle</div></body></html>\n"
            ),
            "tests/test_billing_dom.py": (
                "from pathlib import Path\nimport unittest\n\n"
                "class BillingDomTest(unittest.TestCase):\n"
                "    def test_contains_ledger_landmarks_and_amount(self) -> None:\n"
                "        html = Path('ui/billing.html').read_text(encoding='utf-8')\n"
                "        self.assertIn('id=\"invoice-total\"', html)\n"
                "        self.assertIn('$1,420.00', html)\n"
                "        self.assertIn('<table', html)\n"
            ),
        },
        naive_attempt_files={
            "ui/billing.html": (
                "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<title>NovaBilling — Enterprise Usage</title>"
                "<style>:root{--bg:#F9F7F1;--ink:#161514;}body{background:var(--bg);color:var(--ink);padding:14px;}</style></head>"
                "<body><h1>Usage Ledger <span>Pro</span></h1><div id='invoice-total'>$1,420.00</div><table><tr><td>Compute</td></tr></table></body></html>\n"
            )
        },
        reference_solution_files={
            "ui/billing.html": (
                "<!doctype html>\n<html lang=\"en\">\n<head>\n"
                "  <meta charset=\"utf-8\">\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                "  <meta name=\"description\" content=\"Monthly compute and storage invoice ledger for NovaBilling.\">\n"
                "  <title>NovaBilling: Monthly Usage Ledger</title>\n"
                "  <style>\n"
                "    /* Hallmark · v2.0.0 | theme: newsprint-ledger (custom) | genre: editorial | scope: app | signature: tabular invoice ledger */\n"
                "    /* critique: warm tinted newsprint ledger with 1px rules and tabular financial columns */\n"
                "    :root {\n"
                "      --bg-page: #F9F7F1;\n"
                "      --bg-surface: #FCFBF8;\n"
                "      --line-rule: #DFDCD2;\n"
                "      --ink-main: #161514;\n"
                "      --ink-muted: #57544E;\n"
                "    }\n"
                "    html, body {\n"
                "      margin: 0;\n"
                "      padding: 0;\n"
                "      overflow-x: clip;\n"
                "      background: var(--bg-page);\n"
                "      color: var(--ink-main);\n"
                "      font-family: system-ui, -apple-system, sans-serif;\n"
                "    }\n"
                "    body { padding: 24px; }\n"
                "    h1, h2 { margin: 0 0 8px 0; text-wrap: balance; }\n"
                "    .subtitle { margin: 0 0 20px 0; color: var(--ink-muted); }\n"
                "    .ledger { background: var(--bg-surface); border: 1px solid var(--line-rule); padding: 16px; color: var(--ink-main); }\n"
                "    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-variant-numeric: tabular-nums; }\n"
                "    th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line-rule); }\n"
                "  </style>\n"
                "</head>\n<body>\n"
                "  <main class=\"ledger\">\n"
                "    <h1>Monthly Usage Ledger</h1>\n"
                "    <p class=\"subtitle\">Cycle period: November 2026 (Pro workspace)</p>\n"
                "    <div id=\"invoice-total\"><strong>$1,420.00</strong> current cycle total</div>\n"
                "    <table>\n"
                "      <thead><tr><th>Resource</th><th>Units</th><th>Amount</th></tr></thead>\n"
                "      <tbody><tr><td>GPU Build Minutes</td><td>4,100 min</td><td>$1,420.00</td></tr></tbody>\n"
                "    </table>\n"
                "  </main>\n"
                "</body>\n</html>\n"
            )
        },
        max_diff_lines=70,
    )


def _task_ui_02_incident_board() -> TaskSpec:
    return TaskSpec(
        task_id="ui-02-incident-status-board",
        title="Build zero-sloplint regional incident status board HTML with tabular uptime metrics",
        category="frontend_craft",
        prompt=(
            "Fix `ui/status.html` so all three regions (`us-west`, `eu-central`, `ap-south`) render inside a semantic "
            "`<main>` table with `tabular-nums`, zero em-dashes, strict 4px spacing values, and zero Hallmark warnings."
        ),
        test_command="python3 -m unittest tests/test_status_board.py",
        protected_test_paths=["tests/test_status_board.py"],
        frontend_target_html="ui/status.html",
        seed_files={
            "ui/status.html": (
                "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Status — Live</title>"
                "<style>body{background:#0f172a;color:#fff;padding:15px;}</style></head>"
                "<body><h1>Platform Status — Global</h1><div>us-west operational</div></body></html>\n"
            ),
            "tests/test_status_board.py": (
                "from pathlib import Path\nimport unittest\n\n"
                "class StatusBoardTest(unittest.TestCase):\n"
                "    def test_regions_and_tabular_nums(self) -> None:\n"
                "        html = Path('ui/status.html').read_text(encoding='utf-8')\n"
                "        for region in ('us-west', 'eu-central', 'ap-south'):\n"
                "            self.assertIn(region, html)\n"
                "        self.assertIn('tabular-nums', html)\n"
                "        self.assertIn('<main', html)\n"
            ),
        },
        naive_attempt_files={
            "ui/status.html": (
                "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Status — Live</title>"
                "<style>body{padding:10px;font-variant-numeric:tabular-nums;}</style></head>"
                "<body><main><h1>Status</h1><p>us-west eu-central ap-south</p></main></body></html>\n"
            )
        },
        reference_solution_files={
            "ui/status.html": (
                "<!doctype html>\n<html lang=\"en\">\n<head>\n"
                "  <meta charset=\"utf-8\">\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                "  <meta name=\"description\" content=\"Live regional availability and p99 latency telemetry board.\">\n"
                "  <title>Platform Availability Telemetry</title>\n"
                "  <style>\n"
                "    /* Hallmark · v2.0.0 | theme: newsprint-telemetry (custom) | genre: modern-minimal | scope: app | signature: regional latency matrix */\n"
                "    /* critique: high-contrast regional latency table with warm tinted surface and tabular figures */\n"
                "    :root {\n"
                "      --bg: #F9F7F1;\n"
                "      --card: #FCFBF8;\n"
                "      --border: #DFDCD2;\n"
                "      --ink: #161514;\n"
                "      --ok: #176B47;\n"
                "    }\n"
                "    html, body { margin: 0; padding: 0; overflow-x: clip; background: var(--bg); color: var(--ink); font-family: system-ui, sans-serif; }\n"
                "    body { padding: 24px; }\n"
                "    h1, h2 { margin: 0 0 12px 0; text-wrap: balance; }\n"
                "    main { background: var(--card); border: 1px solid var(--border); padding: 20px; color: var(--ink); }\n"
                "    table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }\n"
                "    th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }\n"
                "    .state-ok { color: var(--ok); font-weight: 600; }\n"
                "  </style>\n"
                "</head>\n<body>\n"
                "  <main>\n"
                "    <h1>Regional Availability Board</h1>\n"
                "    <table>\n"
                "      <thead><tr><th>Region</th><th>Active Sockets</th><th>p99 Latency</th><th>State</th></tr></thead>\n"
                "      <tbody>\n"
                "        <tr><td>us-west</td><td>14,280</td><td>42 ms</td><td class=\"state-ok\">Operational</td></tr>\n"
                "        <tr><td>eu-central</td><td>9,640</td><td>51 ms</td><td class=\"state-ok\">Operational</td></tr>\n"
                "        <tr><td>ap-south</td><td>6,190</td><td>68 ms</td><td class=\"state-ok\">Operational</td></tr>\n"
                "      </tbody>\n"
                "    </table>\n"
                "  </main>\n"
                "</body>\n</html>\n"
            )
        },
        max_diff_lines=70,
    )


def _task_refac_01_cron_parser() -> TaskSpec:
    return TaskSpec(
        task_id="refac-01-cron-expression-parser",
        title="Extend 5-field cron matcher to support step intervals and comma lists",
        category="refactor_test",
        prompt=(
            "Refactor `cron_eval.py` so `expand_field(expr, min_val, max_val)` supports `*`, `*/step`, "
            "`start-end`, `start-end/step`, and comma-separated combinations while raising `ValueError` on out-of-bounds values."
        ),
        test_command="python3 -m unittest tests/test_cron_eval.py",
        protected_test_paths=["tests/test_cron_eval.py"],
        seed_files={
            "cron_eval.py": (
                "def expand_field(expr: str, min_val: int, max_val: int) -> list[int]:\n"
                "    if expr == '*':\n"
                "        return list(range(min_val, max_val + 1))\n"
                "    val = int(expr)\n"
                "    return [val]\n"
            ),
            "tests/test_cron_eval.py": (
                "import unittest\nfrom cron_eval import expand_field\n\n"
                "class CronEvalTest(unittest.TestCase):\n"
                "    def test_steps_and_lists(self) -> None:\n"
                "        self.assertEqual(expand_field('*/15', 0, 59), [0, 15, 30, 45])\n"
                "        self.assertEqual(expand_field('1-7/3,10', 0, 12), [1, 4, 7, 10])\n"
                "    def test_bounds_validation(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            expand_field('0-75/15', 0, 59)\n"
                "        with self.assertRaises(ValueError):\n"
                "            expand_field('*/0', 0, 59)\n"
            ),
        },
        reference_solution_files={
            "cron_eval.py": (
                "def expand_field(expr: str, min_val: int, max_val: int) -> list[int]:\n"
                "    values: set[int] = set()\n"
                "    for part in expr.split(','):\n"
                "        part = part.strip()\n"
                "        step = 1\n"
                "        if '/' in part:\n"
                "            base, step_s = part.split('/', 1)\n"
                "            step = int(step_s)\n"
                "            if step <= 0:\n"
                "                raise ValueError('step must be > 0')\n"
                "        else:\n"
                "            base = part\n"
                "        if base == '*':\n"
                "            lo, hi = min_val, max_val\n"
                "        elif '-' in base:\n"
                "            lo_s, hi_s = base.split('-', 1)\n"
                "            lo, hi = int(lo_s), int(hi_s)\n"
                "        else:\n"
                "            lo = hi = int(base)\n"
                "        if lo < min_val or hi > max_val or lo > hi:\n"
                "            raise ValueError('cron field out of bounds')\n"
                "        values.update(range(lo, hi + 1, step))\n"
                "    return sorted(values)\n"
            )
        },
        max_diff_lines=50,
    )


def _task_refac_02_jsonl_compactor() -> TaskSpec:
    return TaskSpec(
        task_id="refac-02-jsonl-ring-compactor",
        title="Bounded JSONL log compactor preserving newest records under max_records and max_bytes",
        category="refactor_test",
        prompt=(
            "Implement `compact_jsonl(path, max_records, max_bytes)` in `compactor.py` so malformed lines are skipped, "
            "and only the newest valid JSON lines fitting within BOTH `max_records` and `max_bytes` (UTF-8 including newline) remain."
        ),
        test_command="python3 -m unittest tests/test_compactor.py",
        protected_test_paths=["tests/test_compactor.py"],
        seed_files={
            "compactor.py": (
                "from pathlib import Path\n\n"
                "def compact_jsonl(path: Path, max_records: int, max_bytes: int) -> int:\n"
                "    lines = path.read_text(encoding='utf-8').splitlines()\n"
                "    kept = lines[-max_records:]\n"
                "    path.write_text('\\n'.join(kept) + '\\n', encoding='utf-8')\n"
                "    return len(kept)\n"
            ),
            "tests/test_compactor.py": (
                "from pathlib import Path\nimport tempfile\nimport unittest\nfrom compactor import compact_jsonl\n\n"
                "class CompactorTest(unittest.TestCase):\n"
                "    def test_enforces_both_caps_and_skips_corrupt(self) -> None:\n"
                "        with tempfile.TemporaryDirectory() as tmp:\n"
                "            p = Path(tmp) / 'events.jsonl'\n"
                "            p.write_text('{\"i\":1}\\nNOT_JSON\\n{\"i\":2}\\n{\"i\":3,\"pad\":\"xxxx\"}\\n{\"i\":4}\\n', encoding='utf-8')\n"
                "            kept = compact_jsonl(p, max_records=3, max_bytes=32)\n"
                "            self.assertEqual(kept, 2)\n"
                "            self.assertLessEqual(len(p.read_bytes()), 32)\n"
                "            self.assertEqual(p.read_text(encoding='utf-8').splitlines(), ['{\"i\":3,\"pad\":\"xxxx\"}', '{\"i\":4}'])\n"
            ),
        },
        naive_attempt_files={
            "compactor.py": (
                "import json\nfrom pathlib import Path\n\n"
                "def compact_jsonl(path: Path, max_records: int, max_bytes: int) -> int:\n"
                "    valid = []\n"
                "    for ln in path.read_text(encoding='utf-8').splitlines():\n"
                "        try:\n"
                "            json.loads(ln)\n"
                "            valid.append(ln)\n"
                "        except Exception:\n"
                "            pass\n"
                "    kept = valid[-max_records:]\n"
                "    path.write_text('\\n'.join(kept) + '\\n', encoding='utf-8')\n"
                "    return len(kept)\n"
            )
        },
        reference_solution_files={
            "compactor.py": (
                "import json\nfrom pathlib import Path\n\n"
                "def compact_jsonl(path: Path, max_records: int, max_bytes: int) -> int:\n"
                "    valid: list[str] = []\n"
                "    for raw_line in path.read_text(encoding='utf-8').splitlines():\n"
                "        stripped = raw_line.strip()\n"
                "        if not stripped:\n"
                "            continue\n"
                "        try:\n"
                "            json.loads(stripped)\n"
                "            valid.append(stripped)\n"
                "        except json.JSONDecodeError:\n"
                "            continue\n"
                "    selected_rev: list[str] = []\n"
                "    used_bytes = 0\n"
                "    for record in reversed(valid):\n"
                "        if len(selected_rev) >= max_records:\n"
                "            break\n"
                "        rec_bytes = len((record + '\\n').encode('utf-8'))\n"
                "        if used_bytes + rec_bytes > max_bytes:\n"
                "            break\n"
                "        selected_rev.append(record)\n"
                "        used_bytes += rec_bytes\n"
                "    kept = list(reversed(selected_rev))\n"
                "    path.write_text(('\\n'.join(kept) + '\\n') if kept else '', encoding='utf-8')\n"
                "    return len(kept)\n"
            )
        },
        max_diff_lines=50,
    )


def _task_sec_01_dsn_redactor() -> TaskSpec:
    return TaskSpec(
        task_id="sec-01-dsn-credential-redactor",
        title="Sanitize DSN passwords, Bearer tokens, and sk-* API keys in structured logs",
        category="security_hygiene",
        prompt=(
            "Fix `sanitizer.py` so `redact_secrets(text)` masks URI passwords (`postgres://user:pass@host`), "
            "`Bearer <token>` headers, and `sk-...` API keys with `[REDACTED]` without altering safe URLs or usernames."
        ),
        test_command="python3 -m unittest tests/test_sanitizer.py",
        protected_test_paths=["tests/test_sanitizer.py"],
        seed_files={
            "sanitizer.py": (
                "import re\n\n"
                "def redact_secrets(text: str) -> str:\n"
                "    return re.sub(r'sk-[A-Za-z0-9_-]{10,}', '[REDACTED]', text)\n"
            ),
            "tests/test_sanitizer.py": (
                "import unittest\nfrom sanitizer import redact_secrets\n\n"
                "class SanitizerTest(unittest.TestCase):\n"
                "    def test_redacts_dsn_bearer_and_api_keys(self) -> None:\n"
                "        fake_jwt = 'eyJ0eXAi' + '.demoPayload.' + 'demoSig'\n"
                "        fake_key = 'sk-' + 'demo-' + 'ABCDEF1234567890'\n"
                "        fake_pw = 'TestPass' + '_99!'\n"
                "        sample = (\n"
                "            f'db=postgresql://app_user:{fake_pw}@db.internal:5432/prod '\n"
                "            f'auth=\"Bearer {fake_jwt}\" '\n"
                "            f'key={fake_key}'\n"
                "        )\n"
                "        clean = redact_secrets(sample)\n"
                "        self.assertNotIn(fake_pw, clean)\n"
                "        self.assertIn('postgresql://app_user:[REDACTED]@db.internal:5432/prod', clean)\n"
                "        self.assertIn('Bearer [REDACTED]', clean)\n"
                "        self.assertNotIn('ABCDEF1234567890', clean)\n"
            ),
        },
        reference_solution_files={
            "sanitizer.py": (
                "import re\n\n"
                "_DSN_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://[^:\\s/@]+):([^@\\s]+)@')\n"
                "_BEARER_RE = re.compile(r'\\bBearer\\s+[A-Za-z0-9._~+/-]+=*')\n"
                "_SK_RE = re.compile(r'\\bsk-[A-Za-z0-9_-]{10,}')\n\n"
                "def redact_secrets(text: str) -> str:\n"
                "    out = _DSN_RE.sub(r'\\1:[REDACTED]@', text)\n"
                "    out = _BEARER_RE.sub('Bearer [REDACTED]', out)\n"
                "    out = _SK_RE.sub('[REDACTED]', out)\n"
                "    return out\n"
            )
        },
        max_diff_lines=35,
    )


def _task_sec_02_archive_slip() -> TaskSpec:
    return TaskSpec(
        task_id="sec-02-zip-slip-tar-extractor",
        title="Prevent Zip-Slip and symlink escape during tar archive extraction",
        category="security_hygiene",
        prompt=(
            "Harden `safe_extract_tar(tar_path, dest_dir)` in `archive_guard.py` so members using `..`, absolute paths, "
            "or symlinks/hardlinks escaping `dest_dir` raise `PermissionError` before writing any file outside `dest_dir`."
        ),
        test_command="python3 -m unittest tests/test_archive_guard.py",
        protected_test_paths=["tests/test_archive_guard.py"],
        seed_files={
            "archive_guard.py": (
                "from pathlib import Path\nimport tarfile\n\n"
                "def safe_extract_tar(tar_path: Path, dest_dir: Path) -> list[Path]:\n"
                "    dest_dir.mkdir(parents=True, exist_ok=True)\n"
                "    extracted: list[Path] = []\n"
                "    with tarfile.open(tar_path, 'r:*') as tf:\n"
                "        tf.extractall(path=dest_dir)\n"
                "        for m in tf.getmembers():\n"
                "            extracted.append(dest_dir / m.name)\n"
                "    return extracted\n"
            ),
            "tests/test_archive_guard.py": (
                "import io\nfrom pathlib import Path\nimport tarfile\nimport tempfile\nimport unittest\n"
                "from archive_guard import safe_extract_tar\n\n"
                "class ArchiveGuardTest(unittest.TestCase):\n"
                "    def test_blocks_parent_traversal_and_symlink_escape(self) -> None:\n"
                "        with tempfile.TemporaryDirectory() as tmp:\n"
                "            root = Path(tmp)\n"
                "            evil_tar = root / 'evil.tar'\n"
                "            with tarfile.open(evil_tar, 'w') as tf:\n"
                "                info = tarfile.TarInfo(name='../escaped.txt')\n"
                "                data = b'pwned'\n"
                "                info.size = len(data)\n"
                "                tf.addfile(info, io.BytesIO(data))\n"
                "            with self.assertRaises(PermissionError):\n"
                "                safe_extract_tar(evil_tar, root / 'out')\n"
                "            self.assertFalse((root / 'escaped.txt').exists())\n\n"
                "            sym_tar = root / 'sym.tar'\n"
                "            with tarfile.open(sym_tar, 'w') as tf:\n"
                "                s = tarfile.TarInfo(name='link_out')\n"
                "                s.type = tarfile.SYMTYPE\n"
                "                s.linkname = '/tmp'\n"
                "                tf.addfile(s)\n"
                "            with self.assertRaises(PermissionError):\n"
                "                safe_extract_tar(sym_tar, root / 'out2')\n"
            ),
        },
        naive_attempt_files={
            "archive_guard.py": (
                "from pathlib import Path\nimport tarfile\n\n"
                "def safe_extract_tar(tar_path: Path, dest_dir: Path) -> list[Path]:\n"
                "    dest_dir.mkdir(parents=True, exist_ok=True)\n"
                "    with tarfile.open(tar_path, 'r:*') as tf:\n"
                "        for m in tf.getmembers():\n"
                "            if '..' in m.name:\n"
                "                raise PermissionError('unsafe path')\n"
                "        tf.extractall(path=dest_dir)\n"
                "        return [dest_dir / m.name for m in tf.getmembers()]\n"
            )
        },
        reference_solution_files={
            "archive_guard.py": (
                "from pathlib import Path\nimport tarfile\n\n"
                "def _ensure_within(base_resolved: Path, target: Path) -> Path:\n"
                "    resolved = target.resolve(strict=False)\n"
                "    if resolved != base_resolved and base_resolved not in resolved.parents:\n"
                "        raise PermissionError(f'Archive entry escapes destination: {target}')\n"
                "    return resolved\n\n"
                "def safe_extract_tar(tar_path: Path, dest_dir: Path) -> list[Path]:\n"
                "    dest_dir.mkdir(parents=True, exist_ok=True)\n"
                "    base = dest_dir.resolve()\n"
                "    extracted: list[Path] = []\n"
                "    with tarfile.open(tar_path, 'r:*') as tf:\n"
                "        members = tf.getmembers()\n"
                "        for m in members:\n"
                "            if m.name.startswith('/') or '..' in Path(m.name).parts:\n"
                "                raise PermissionError(f'Illegal archive member path: {m.name}')\n"
                "            target = _ensure_within(base, base / m.name)\n"
                "            if m.issym() or m.islnk():\n"
                "                link_target = (target.parent / m.linkname) if not m.linkname.startswith('/') else Path(m.linkname)\n"
                "                _ensure_within(base, link_target)\n"
                "            if m.isdev():\n"
                "                raise PermissionError('Special device files are forbidden')\n"
                "        for m in members:\n"
                "            target = base / m.name\n"
                "            if m.isdir():\n"
                "                target.mkdir(parents=True, exist_ok=True)\n"
                "            elif m.isfile():\n"
                "                target.parent.mkdir(parents=True, exist_ok=True)\n"
                "                src = tf.extractfile(m)\n"
                "                if src is not None:\n"
                "                    target.write_bytes(src.read())\n"
                "                    extracted.append(target)\n"
                "    return extracted\n"
            )
        },
        max_diff_lines=65,
    )
