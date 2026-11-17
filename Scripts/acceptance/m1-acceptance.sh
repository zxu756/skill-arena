#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=Scripts/_bootstrap_env.sh
source "${ROOT_DIR}/Scripts/_bootstrap_env.sh"

RECORD=0
for arg in "$@"; do
  if [[ "$arg" == "--record" ]]; then
    RECORD=1
  else
    echo "Unknown argument: $arg" >&2
    exit 2
  fi
done

REPORT_FILE="${ROOT_DIR}/reports/m1-acceptance.txt"

TMP_WORK="$(mktemp -d "${TMPDIR:-/tmp}/skill-arena-m1-acc.XXXXXX")"
cleanup() {
  rm -rf "$TMP_WORK"
}
trap cleanup EXIT

export BROWSER=true
export HOME="${TMP_WORK}/outer-caller-home"
mkdir -p "$HOME" "${TMP_WORK}/bin"

run_suite() {
  echo "================================================================"
  echo "SkillArena Milestone M1 Acceptance Suite (Isolated HOME + relay.sh + agent-xray + 95% CI)"
  echo "Workspace: <REPO_ROOT> (branch: $(git rev-parse --abbrev-ref HEAD))"
  echo "================================================================"

  # Prepare a single fast concurrency task & reference solution
  ONE_TASK_JSON="${TMP_WORK}/task_conc01.json"
  REF_SOLUTION_FILE="${TMP_WORK}/ref_rate_limiter.py"
  "$PYTHON" - "$ONE_TASK_JSON" "$REF_SOLUTION_FILE" <<'PY'
import json, sys
from pathlib import Path
from skill_arena.suites.builtin_tasks import load_builtin_suite

t = load_builtin_suite(category="concurrency")[0]
d = t.to_dict()
d["timeout_sec"] = 8
# Also seed a pre-existing workspace skill inside seed_files to prove Control strips workspace skills too
d["seed_files"][".agents/skills/preexisting-repo-skill/SKILL.md"] = "# Preexisting Repo Skill\nShould be stripped in Control.\n"
d["seed_files"][".claude/skills/preexisting-repo-skill/SKILL.md"] = "# Preexisting Repo Skill\nShould be stripped in Control.\n"
Path(sys.argv[1]).write_text(json.dumps(d, indent=2), encoding="utf-8")
Path(sys.argv[2]).write_text(t.reference_solution_files["rate_limiter.py"], encoding="utf-8")
PY

  CANDIDATE_SKILL_DIR="${TMP_WORK}/candidate-alpha"
  mkdir -p "$CANDIDATE_SKILL_DIR"
  cat > "${CANDIDATE_SKILL_DIR}/SKILL.md" <<'MD'
---
name: candidate-alpha
description: Deterministic M1 acceptance test skill.
---
# Candidate Alpha Skill
Always guard shared token-bucket state with a threading.Lock.
MD

  # Also populate outer caller HOME with decoy skills to prove outer HOME skills never leak into trials
  mkdir -p "${HOME}/.agents/skills/outer-decoy-skill" "${HOME}/.claude/skills/outer-decoy-skill"
  printf "# Outer Decoy\n" > "${HOME}/.agents/skills/outer-decoy-skill/SKILL.md"
  printf "# Outer Decoy\n" > "${HOME}/.claude/skills/outer-decoy-skill/SKILL.md"

  # ------------------------------------------------------------------
  # Row 1 (M1-1): Stub prints HOME & global/workspace skill dirs -> Control sees 0, Treatment sees ONLY candidate
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 1/7] Per-trial isolated HOME & skill directories (Control=0 skills, Treatment=only candidate)..."
  OBS_DIR="${TMP_WORK}/row1_obs"
  mkdir -p "$OBS_DIR"
  cat > "${TMP_WORK}/bin/claude" <<PY
#!${PYTHON}
import json, os, shutil, sys, uuid
from pathlib import Path

_ = sys.stdin.read()
home = Path(os.environ["HOME"])
cwd = Path.cwd()

def collect_skill_mds(base: Path, roots: tuple[str, ...]) -> list[str]:
    found: set[str] = set()
    for sub in roots:
        target = base / sub
        if not target.exists():
            continue
        for dirpath, _, filenames in os.walk(target, followlinks=True):
            if "SKILL.md" in filenames:
                found.add(os.path.relpath(os.path.join(dirpath, "SKILL.md"), base))
    return sorted(found)

global_skill_mds = collect_skill_mds(
    home, (".agents/skills", ".claude/skills", ".codex/skills", ".config/opencode/skills")
)
workspace_skill_mds = collect_skill_mds(
    cwd, (".agents/skills", ".claude/skills", ".codex/skills", ".opencode/skills")
)
record = {
    "contender": os.environ.get("SKILL_ARENA_CONTENDER"),
    "home": str(home),
    "tmpdir": os.environ.get("TMPDIR"),
    "xdg_config_home": os.environ.get("XDG_CONFIG_HOME"),
    "xdg_data_home": os.environ.get("XDG_DATA_HOME"),
    "xdg_state_home": os.environ.get("XDG_STATE_HOME"),
    "xdg_cache_home": os.environ.get("XDG_CACHE_HOME"),
    "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "codex_home": os.environ.get("CODEX_HOME"),
    "vibe_home": os.environ.get("VIBE_HOME"),
    "ssh_auth_sock_present": "SSH_AUTH_SOCK" in os.environ,
    "claude_code_keys": sorted(k for k in os.environ if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"),
    "api_key_vars": sorted(k for k in os.environ if k.endswith("_API_KEY")),
    "leaked_secret_present": "RANDOM_HOST_SECRET" in os.environ,
    "global_skill_mds": global_skill_mds,
    "workspace_skill_mds": workspace_skill_mds,
}
obs_file = Path("${OBS_DIR}") / f"{record['contender']}_{uuid.uuid4().hex[:6]}.json"
obs_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
shutil.copyfile("${REF_SOLUTION_FILE}", "rate_limiter.py")
print(json.dumps({"result": "Verified isolated HOME.\nSTATUS: success", "total_cost_usd": 0.01, "session_id": "r1"}))
PY
  chmod +x "${TMP_WORK}/bin/claude"

  ROW1_DB="${TMP_WORK}/row1.sqlite"
  SSH_AUTH_SOCK="/tmp/fake-ssh.sock" CLAUDE_CODE_MESSAGING_TOKEN="leak-test" RANDOM_HOST_SECRET="leak-test" \
    ANTHROPIC_API_KEY="fake-ant" OPENAI_API_KEY="fake-oai" MISTRAL_API_KEY="fake-mis" \
    GOOGLE_API_KEY="fake-goo" GEMINI_API_KEY="fake-gem" \
    RELAY_CLAUDE_BIN="${TMP_WORK}/bin/claude" PATH="${TMP_WORK}/bin:${PATH}" \
    "$PYTHON" -m skill_arena.cli --lang en run \
    --cli claude --model claude-sonnet-4-5 \
    --skill "$CANDIDATE_SKILL_DIR" \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$ROW1_DB" >/dev/null

  "$PYTHON" - "$OBS_DIR" "$HOME" <<'PY'
import json, os, pwd, sys
from pathlib import Path

obs_dir = Path(sys.argv[1])
outer_home = Path(sys.argv[2]).resolve()
real_pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()

records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(obs_dir.glob("*.json"))]
assert len(records) == 2, records
by_arm = {r["contender"]: r for r in records}
ctrl = by_arm["arm_control"]
treat = by_arm["arm_treatment"]

for r in (ctrl, treat):
    h = Path(r["home"]).resolve()
    assert h != real_pw_home, f"Trial HOME matched real user HOME: {h}"
    assert h != outer_home, f"Trial HOME matched outer caller HOME: {h}"
    assert r["xdg_config_home"] == f"{r['home']}/.config", r
    assert r["xdg_data_home"] == f"{r['home']}/.local/share", r
    assert r["xdg_state_home"] == f"{r['home']}/.local/state", r
    assert r["xdg_cache_home"] == f"{r['home']}/.cache", r
    assert r["claude_config_dir"] == f"{r['home']}/.claude", r
    assert r["codex_home"] == f"{r['home']}/.codex", r
    assert r["vibe_home"] == f"{r['home']}/.vibe", r
    assert r["ssh_auth_sock_present"] is False, f"SSH_AUTH_SOCK leaked into child env: {r}"
    assert r["claude_code_keys"] == [], f"CLAUDE_CODE_* leaked into child env: {r['claude_code_keys']}"
    assert r["api_key_vars"] == [], f"Default run forwarded API keys without --pass-env: {r['api_key_vars']}"
    assert r["leaked_secret_present"] is False, f"Host env leaked into child env: {r}"
    assert r["tmpdir"] and not Path(r["tmpdir"]).exists(), f"Per-trial TMPDIR not cleaned up: {r['tmpdir']}"
    assert not Path(r["home"]).exists(), f"Temporary trial HOME was not cleaned up: {r['home']}"

assert ctrl["home"] != treat["home"], "Control and Treatment must not share the same trial HOME"
assert ctrl["global_skill_mds"] == [], f"Control saw leaked global skills: {ctrl['global_skill_mds']}"
assert ctrl["workspace_skill_mds"] == [], f"Control saw leaked workspace skills: {ctrl['workspace_skill_mds']}"

assert treat["global_skill_mds"] == [
    ".agents/skills/candidate-alpha/SKILL.md",
    ".claude/skills/candidate-alpha/SKILL.md",
    ".codex/skills/candidate-alpha/SKILL.md",
], f"Unexpected Treatment global skills: {treat['global_skill_mds']}"
assert treat["workspace_skill_mds"] == [
    ".agents/skills/candidate-alpha/SKILL.md",
    ".claude/skills/candidate-alpha/SKILL.md",
], f"Unexpected Treatment workspace skills: {treat['workspace_skill_mds']}"

print(
    "  ROW 1 [PASS]: Control global_skills=0, workspace_skills=0; "
    "Treatment saw ONLY 'candidate-alpha' (0 host skills leaked); isolated HOMEs distinct & cleaned up."
)
PY

  # ------------------------------------------------------------------
  # Row 2 (M1-2 + Item 2 K9 + KR1): Stub writes unique markers into $HOME/.claude & $HOME/.agents/skills -> lands in isolated HOME; real ~/.claude & ~/.agents/skills lack markers; relay.sh + pinned agent-xray end-to-end
  # ------------------------------------------------------------------
  echo ""
  echo '[Row 2/7] Marker-file isolation probe ($HOME/.claude/kr1_probe_<uuid>.marker & $HOME/.agents/skills/kr1_probe_<uuid>/SKILL.md) + relay.sh & pinned agent-xray check...'
  "$PYTHON" - "$ROOT_DIR" "$TMP_WORK" "$ONE_TASK_JSON" "$CANDIDATE_SKILL_DIR" "$REF_SOLUTION_FILE" <<'PY'
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import threading

from skill_arena.runners.xray_bridge import PINNED_AGENT_XRAY_COMMIT, get_agent_xray_commit

repo_root = Path(sys.argv[1]).resolve()
tmp_work = Path(sys.argv[2]).resolve()
task_json = Path(sys.argv[3]).resolve()
skill_dir = Path(sys.argv[4]).resolve()
ref_sol = Path(sys.argv[5]).resolve()
real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
outer_home = Path(os.environ["HOME"]).resolve()

# Verify pinned agent-xray commit (3cd9b14dcbae54505c56f982d13fae862d606e5a)
assert PINNED_AGENT_XRAY_COMMIT == "3cd9b14dcbae54505c56f982d13fae862d606e5a", PINNED_AGENT_XRAY_COMMIT
resolved_xray_sha = get_agent_xray_commit()
if resolved_xray_sha is not None:
    assert resolved_xray_sha == PINNED_AGENT_XRAY_COMMIT, (resolved_xray_sha, PINNED_AGENT_XRAY_COMMIT)

# Verify K7/K9 deletions: wire_tap_proxy.py deleted, no custom --dangerously-skip-permissions or second proxy
assert not (repo_root / "src/skill_arena/runners/wire_tap_proxy.py").exists(), "wire_tap_proxy.py still exists!"
for py_path in (repo_root / "src/skill_arena").rglob("*.py"):
    txt = py_path.read_text(encoding="utf-8")
    assert "--dangerously-skip-permissions" not in txt, f"Found --dangerously-skip-permissions in {py_path}"
    assert "def build_cli_command" not in txt, f"Found build_cli_command in {py_path}"
    if py_path.name != "server.py":
        assert "BaseHTTPRequestHandler" not in txt, f"Unexpected HTTP server handler in {py_path}"


class _FakeAnthropicUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        payload = json.dumps(
            {
                "id": "msg_row2",
                "type": "message",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 300,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 60,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropicUpstream)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()

probe_log = tmp_work / "row2_probe_writes.jsonl"
stub_claude = tmp_work / "bin" / "claude_row2"
stub_claude.write_text(
    f"""#!{sys.executable}
import json, os, shutil, sys, urllib.request, uuid
from pathlib import Path

_ = sys.stdin.read()
home = Path(os.environ["HOME"])
probe_uuid = uuid.uuid4().hex
claude_marker = home / ".claude" / f"kr1_probe_{{probe_uuid}}.marker"
skill_marker = home / ".agents" / "skills" / f"kr1_probe_{{probe_uuid}}" / "SKILL.md"
claude_marker.parent.mkdir(parents=True, exist_ok=True)
skill_marker.parent.mkdir(parents=True, exist_ok=True)
claude_marker.write_text("written-into-isolated-claude-dir\\n", encoding="utf-8")
skill_marker.write_text("# written-into-isolated-skills-dir\\n", encoding="utf-8")

# Confirm outer RELAY_WORKER was stripped before invoking relay.sh (relay.sh sets its own step ID)
assert os.environ.get("RELAY_WORKER") != "outer_conductor_step", os.environ.get("RELAY_WORKER")

base_url = os.environ["ANTHROPIC_BASE_URL"].rstrip("/")
req = urllib.request.Request(
    f"{{base_url}}/v1/messages",
    data=json.dumps({{"model": "claude-sonnet-4-5", "messages": [{{"role": "user", "content": "fix"}}]}}).encode("utf-8"),
    headers={{"Content-Type": "application/json", "x-api-key": "stub-key"}},
    method="POST",
)
with urllib.request.urlopen(req, timeout=5) as resp:
    assert resp.status == 200

with open({str(probe_log)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "probe_uuid": probe_uuid,
        "trial_home": str(home),
        "claude_marker_exists_during_trial": claude_marker.is_file(),
        "skill_marker_exists_during_trial": skill_marker.is_file(),
    }}) + "\\n")

shutil.copyfile({str(ref_sol)!r}, "rate_limiter.py")
print(json.dumps({{
    "result": "Applied thread-safe fix.\\nSTATUS: success",
    "total_cost_usd": 0.015,
    "session_id": "stub-row2",
    "modelUsage": {{"claude-sonnet-4-5": {{}}}},
}}))
""",
    encoding="utf-8",
)
stub_claude.chmod(0o755)

row2_db = tmp_work / "row2.sqlite"
env = os.environ.copy()
env["RELAY_CLAUDE_BIN"] = str(stub_claude)
env["RELAY_WORKER"] = "outer_conductor_step"
env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"

try:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "skill_arena.cli",
            "--lang",
            "en",
            "run",
            "--cli",
            "claude",
            "--model",
            "claude-sonnet-4-5",
            "--skill",
            str(skill_dir),
            "--budget-usd",
            "1.50",
            "--suite",
            str(task_json),
            "--reps",
            "1",
            "--db",
            str(row2_db),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"CLI exited {proc.returncode}: {proc.stdout}\n{proc.stderr}"
finally:
    srv.shutdown()
    srv.server_close()

probes = [json.loads(line) for line in probe_log.read_text(encoding="utf-8").splitlines() if line.strip()]
assert len(probes) == 2, probes
for p in probes:
    uid = p["probe_uuid"]
    assert p["claude_marker_exists_during_trial"] is True, p
    assert p["skill_marker_exists_during_trial"] is True, p
    assert not (real_home / ".claude" / f"kr1_probe_{uid}.marker").exists(), f"Marker leaked into real ~/.claude: {uid}"
    assert not (real_home / ".agents" / "skills" / f"kr1_probe_{uid}").exists(), f"Marker leaked into real ~/.agents/skills: {uid}"
    assert not (outer_home / ".claude" / f"kr1_probe_{uid}.marker").exists(), f"Marker leaked into outer HOME/.claude: {uid}"
    assert not (outer_home / ".agents" / "skills" / f"kr1_probe_{uid}").exists(), f"Marker leaked into outer HOME/.agents/skills: {uid}"

from skill_arena.store.arena_db import ArenaDatabase

db = ArenaDatabase(row2_db)
exp_id = db.latest_experiment_id()
assert exp_id is not None
row2_report = db.build_comparison_report(exp_id)
assert "$0.0150" in proc.stdout and "Relay LOG" in proc.stdout, proc.stdout
for arm_summary in row2_report["arms"]:
    assert arm_summary["metered_cost_usd"] is not None and arm_summary["metered_cost_usd"] > 0, arm_summary
    assert arm_summary["relay_cost_usd"] == 0.015, arm_summary
trials = db.get_trials(exp_id)
assert len(trials) == 2
for tr in trials:
    assert tr.passed is True, tr.verdict
    assert tr.usage.is_metered is True, tr.usage
    assert tr.usage.uncached_input_tokens == 300, tr.usage
    assert tr.usage.cached_prompt_tokens == 100, tr.usage
    assert tr.usage.total_prompt_tokens == 400, tr.usage
    assert tr.usage.completion_tokens == 60, tr.usage
    assert tr.usage.relay_status == "ok", tr.usage
    assert tr.usage.relay_cost_usd == 0.015, tr.usage
    assert ".relay" not in tr.git_diff, tr.git_diff

print(
    f"  ROW 2 [PASS]: Stub wrote 2x kr1_probe_<uuid> markers inside isolated HOME "
    f"(.claude/*.marker & .agents/skills/*/SKILL.md verified True during trial); "
    f"0 markers in real ~/.claude or ~/.agents/skills; pinned agent-xray={PINNED_AGENT_XRAY_COMMIT[:10]}; "
    f"relay.sh status=ok, relay_cost=$0.0150, agent-xray tokens=(300+100 in, 60 out), .relay cleaned."
)
PY

  # ------------------------------------------------------------------
  # Row 3 (M1-3, K7): Fake SSE upstream sends 1 event every 200ms -> |recorded_ttft_ms - actual_ttft_ms| <= 50ms
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 3/7] Fake SSE upstream emitting 1 event every 200ms (TTFT error <= 50ms)..."
  "$PYTHON" - <<'PY'
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import statistics
import threading
import time
import urllib.request

from skill_arena.runners.xray_bridge import XRayMeteringBridge

server_first_chunk_delays_ms: list[float] = []


class _CadenceSSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        req_arrived = time.perf_counter()
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        events = [
            b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5","usage":{"input_tokens":120,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":0}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"chunk-1"}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"chunk-2"}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"chunk-3"}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":32}}\n\n',
        ]
        for idx, chunk in enumerate(events):
            time.sleep(0.200)
            if idx == 0:
                server_first_chunk_delays_ms.append((time.perf_counter() - req_arrived) * 1000.0)
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, fmt: str, *args: object) -> None:
        return


srv = ThreadingHTTPServer(("127.0.0.1", 0), _CadenceSSEHandler)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
upstream_url = f"http://127.0.0.1:{srv.server_address[1]}"

abs_errors_ms: list[float] = []
recorded_ttfts_ms: list[float] = []
total_latencies_ms: list[int] = []

try:
    for trial_idx in range(5):
        bridge = XRayMeteringBridge(anthropic_upstream=upstream_url, openai_upstream=upstream_url)
        bridge.start()
        try:
            req = urllib.request.Request(
                f"{bridge.anthropic_base_url}/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-api-key": "stub"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                assert b"message_delta" in body
            ledger, records = bridge.summarize()
        finally:
            bridge.stop()

        actual_ms = server_first_chunk_delays_ms[trial_idx]
        rec_ms = ledger.ttft_ms
        err_ms = abs(rec_ms - actual_ms)
        abs_errors_ms.append(err_ms)
        recorded_ttfts_ms.append(rec_ms)
        total_latencies_ms.append(ledger.total_latency_ms)
        assert err_ms <= 50.0, f"Trial {trial_idx + 1}: |recorded={rec_ms:.2f}ms - actual={actual_ms:.2f}ms| = {err_ms:.2f}ms > 50ms"
        assert rec_ms < 350.0, f"Buffered SSE regression: recorded_ttft_ms={rec_ms:.2f}ms"
        assert ledger.total_latency_ms >= 950, f"Expected ~1000ms total stream latency, got {ledger.total_latency_ms}ms"
finally:
    srv.shutdown()
    srv.server_close()

print(
    f"  ROW 3 [PASS]: 5x 5-event SSE streams @ 200ms cadence -> "
    f"recorded_ttft_ms={[round(x, 2) for x in recorded_ttfts_ms]}, "
    f"actual_first_chunk_ms={[round(x, 2) for x in server_first_chunk_delays_ms]}, "
    f"min_abs_err={min(abs_errors_ms):.2f}ms, median_abs_err={statistics.median(abs_errors_ms):.2f}ms, "
    f"max_abs_err={max(abs_errors_ms):.2f}ms (<= 50.0 ms), total_stream_ms={total_latencies_ms}"
)
PY

  # ------------------------------------------------------------------
  # Row 4 (M1-4, K8): OpenAI Responses API (/v1/responses) streaming SSE & non-streaming JSON usage
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 4/7] OpenAI Responses API (/v1/responses) streaming SSE & non-streaming JSON usage..."
  "$PYTHON" - <<'PY'
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

from skill_arena.runners.xray_bridge import XRayMeteringBridge


class _ResponsesOracleUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        req_json = json.loads(self.rfile.read(length).decode("utf-8"))
        usage_obj = {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 800},
            "output_tokens": 150,
            "total_tokens": 1350,
        }
        if req_json.get("stream"):
            sse = (
                'event: response.created\n'
                'data: {"type":"response.created","response":{"id":"resp_sse","model":"gpt-5"}}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"fixed"}\n\n'
                'event: response.completed\n'
                + "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_sse",
                            "model": "gpt-5",
                            "status": "completed",
                            "usage": usage_obj,
                        },
                    }
                )
                + "\n\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(sse)
            self.wfile.flush()
        else:
            body = json.dumps(
                {
                    "id": "resp_json",
                    "object": "response",
                    "model": "gpt-5",
                    "status": "completed",
                    "usage": usage_obj,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


srv = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesOracleUpstream)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
upstream_url = f"http://127.0.0.1:{srv.server_address[1]}"

# Hand-calculated gpt-5 turn cost:
# uncached_input = 1200 - 800 = 400 @ $1.25/MTok = $0.000500
# cached_input   = 800          @ $0.125/MTok   = $0.000100
# output         = 150          @ $10.00/MTok   = $0.001500
# exact sum      = $0.002100
EXPECTED_COST = 0.0021

try:
    for mode, is_stream in (("streaming_sse", True), ("non_streaming_json", False)):
        bridge = XRayMeteringBridge(anthropic_upstream=upstream_url, openai_upstream=upstream_url)
        bridge.start()
        try:
            req = urllib.request.Request(
                f"{bridge.openai_base_url}/responses",
                data=json.dumps({"model": "gpt-5", "stream": is_stream, "input": "patch bug"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer stub-key"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
            ledger, records = bridge.summarize()
        finally:
            bridge.stop()

        assert ledger.is_metered is True, (mode, ledger)
        assert ledger.uncached_input_tokens == 400, (mode, ledger)
        assert ledger.cached_prompt_tokens == 800, (mode, ledger)
        assert ledger.total_prompt_tokens == 1200, (mode, ledger)
        assert ledger.completion_tokens == 150, (mode, ledger)
        assert ledger.total_cost_usd is not None and abs(ledger.total_cost_usd - EXPECTED_COST) < 1e-6, (mode, ledger)
finally:
    srv.shutdown()
    srv.server_close()

print(
    "  ROW 4 [PASS]: /v1/responses streaming SSE & non-streaming JSON both recorded exact "
    "uncached_input=400, cached_input=800, total_prompt=1200, output=150, cost_usd=$0.002100."
)
PY

  # ------------------------------------------------------------------
  # Row 5 (M1-5, K10): Equal true pass rate, 20 trials per arm -> "看不出差别" / "Inconclusive" & no winner rank
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 5/7] Equal pass rate across 20 trials per arm (40 trials via relay.sh) -> '看不出差别' / 'Inconclusive'..."
  ROW5_COUNTER_DIR="${TMP_WORK}/row5_counts"
  mkdir -p "$ROW5_COUNTER_DIR"
  cat > "${TMP_WORK}/bin/claude_row5" <<PY
#!${PYTHON}
import json, os, shutil, sys
from pathlib import Path

_ = sys.stdin.read()
arm = os.environ.get("SKILL_ARENA_CONTENDER", "unknown")
cnt_file = Path("${ROW5_COUNTER_DIR}") / f"{arm}.count"
idx = int(cnt_file.read_text(encoding="utf-8")) if cnt_file.exists() else 0
cnt_file.write_text(str(idx + 1), encoding="utf-8")

# Identical 50% true pass rate (10/20 passed) in both Control and Treatment
if idx % 2 == 0:
    shutil.copyfile("${REF_SOLUTION_FILE}", "rate_limiter.py")
else:
    Path("rate_limiter.py").write_text("class TokenBucket:\n    pass\n", encoding="utf-8")

print(json.dumps({"result": "Completed trial.\nSTATUS: success", "total_cost_usd": 0.005, "session_id": f"{arm}-{idx}"}))
PY
  chmod +x "${TMP_WORK}/bin/claude_row5"

  ROW5_DB="${TMP_WORK}/row5_20x20.sqlite"
  ROW5_HTML_ZH="${TMP_WORK}/row5_zh.html"
  ROW5_HTML_EN="${TMP_WORK}/row5_en.html"

  ROW5_OUT_ZH="$(RELAY_CLAUDE_BIN="${TMP_WORK}/bin/claude_row5" PATH="${TMP_WORK}/bin:${PATH}" \
    "$PYTHON" -m skill_arena.cli --lang zh run \
    --cli claude --model claude-sonnet-4-5 \
    --skill "$CANDIDATE_SKILL_DIR" \
    --suite "$ONE_TASK_JSON" --reps 20 \
    --db "$ROW5_DB" --html-out "$ROW5_HTML_ZH")"
  echo "$ROW5_OUT_ZH" | grep -q "看不出差别"

  ROW5_OUT_EN="$("$PYTHON" -m skill_arena.cli --lang en compare --db "$ROW5_DB")"
  echo "$ROW5_OUT_EN" | grep -q "Inconclusive"

  "$PYTHON" -m skill_arena.cli --lang en export-html --db "$ROW5_DB" --out "$ROW5_HTML_EN" >/dev/null

  "$PYTHON" - "$ROW5_DB" "$ROW5_HTML_ZH" "$ROW5_HTML_EN" <<'PY'
from pathlib import Path
import sys
from skill_arena.graders.frontend_craft import audit_html_craft
from skill_arena.store.arena_db import ArenaDatabase

db = ArenaDatabase(Path(sys.argv[1]))
html_zh_path = Path(sys.argv[2])
html_en_path = Path(sys.argv[3])
html_zh = html_zh_path.read_text(encoding="utf-8")
html_en = html_en_path.read_text(encoding="utf-8")

exp_id = db.latest_experiment_id()
assert exp_id is not None
report = db.build_comparison_report(exp_id)
stat = report["statistical_comparison"]
assert stat["control_trials"] == 20 and stat["treatment_trials"] == 20, stat
assert stat["control_passed"] == 10 and stat["treatment_passed"] == 10, stat
assert stat["delta_pass_rate"] == 0.0, stat
assert stat["ci_low"] < 0.0 < stat["ci_high"], stat
assert stat["verdict"] == "inconclusive", stat
assert stat["winner"] is None, stat
assert "看不出差别" in stat["verdict_label_zh"], stat
assert "Inconclusive" in stat["verdict_label_en"], stat

for row in report["leaderboard"]:
    assert row["rank"] is None, f"Inconclusive run must not assign rank: {row}"

assert "看不出差别" in html_zh
assert "Inconclusive" in html_en

for label, hpath in (("zh", html_zh_path), ("en", html_en_path)):
    craft = audit_html_craft(hpath)
    assert craft.passed and craft.fails == 0 and craft.warns == 0, (label, craft.findings)

print(
    f"  ROW 5 [PASS]: 20 vs 20 equal-rate trials (10/20 vs 10/20) -> "
    f"delta={stat['delta_pass_rate']:+.4f}, 95% Newcombe-Wilson CI=[{stat['ci_low']:+.4f}, {stat['ci_high']:+.4f}], "
    f"verdict='inconclusive' ('看不出差别' / 'Inconclusive'), winner=None, ranks=[None, None], HTML craft 0 warnings."
)
PY

  # ------------------------------------------------------------------
  # Part 2 Addition (07-Part2-K13): Detached double-fork daemon check repeated under isolated HOME
  # ------------------------------------------------------------------
  echo ""
  echo "[Part 2 / K13] Detached double-fork + setsid + close-fds + chdir('/') check under isolated HOME..."
  K13_PIDS="${TMP_WORK}/k13_isolated_pids.txt"
  cat > "${TMP_WORK}/bin/claude_k13" <<PY
#!${PYTHON}
import json, os, shutil, sys
from pathlib import Path

_ = sys.stdin.read()
assert os.environ.get("SKILL_ARENA_ISOLATED_HOME") == "1", os.environ.get("SKILL_ARENA_ISOLATED_HOME")
shutil.copyfile("${REF_SOLUTION_FILE}", "rate_limiter.py")
pid1 = os.fork()
if pid1 == 0:
    os.setsid()
    pid2 = os.fork()
    if pid2 == 0:
        os.chdir("/")
        for fd in range(3, 256):
            try:
                os.close(fd)
            except OSError:
                pass
        dn = os.open(os.devnull, os.O_RDWR)
        os.dup2(dn, 0); os.dup2(dn, 1); os.dup2(dn, 2)
        if dn > 2:
            os.close(dn)
        with Path("${K13_PIDS}").open("a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(120)"])
    os._exit(0)
os.waitpid(pid1, 0)
print(json.dumps({"result": "Spawned daemon.\nSTATUS: success"}))
PY
  chmod +x "${TMP_WORK}/bin/claude_k13"

  K13_DB="${TMP_WORK}/k13_isolated.sqlite"
  K13_T0="$("$PYTHON" -c 'import time; print(time.monotonic())')"
  RELAY_CLAUDE_BIN="${TMP_WORK}/bin/claude_k13" PATH="${TMP_WORK}/bin:${PATH}" \
    "$PYTHON" -m skill_arena.cli --lang en run \
    --cli claude --model claude-sonnet-4-5 \
    --skill "$CANDIDATE_SKILL_DIR" \
    --suite "$ONE_TASK_JSON" --reps 1 --db "$K13_DB" >/dev/null

  "$PYTHON" - "$K13_T0" "$K13_PIDS" "$K13_DB" <<'PY'
import json, os, signal, sqlite3, sys, time

elapsed = time.monotonic() - float(sys.argv[1])
pids = [int(x) for x in open(sys.argv[2], encoding="utf-8").read().splitlines() if x.strip()]
try:
    assert elapsed < 12.0, f"K13 under isolated HOME stalled ({elapsed:.2f}s)"
    assert len(pids) == 2, pids
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
    assert not alive, f"Detached non-SIP PIDs survived under isolated HOME: {alive}"
    conn = sqlite3.connect(sys.argv[3])
    rows = conn.execute("SELECT passed, verdict_json FROM trials").fetchall()
    assert len(rows) == 2, rows
    for (passed, vjson), expected_pid in zip(rows, pids):
        v = json.loads(vjson)
        assert passed == 0, f"Trial with leaked daemon must FAIL: {v}"
        leaked_pids = {int(item["pid"]) for item in v.get("leaked_processes", [])}
        assert expected_pid in leaked_pids, (expected_pid, v)
finally:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
print(f"  PART2-K13 [PASS]: Under isolated HOME, double-fork daemons {pids} killed, recorded in leaked_processes, and trials marked FAILED ({elapsed:.2f}s).")
PY

  # ------------------------------------------------------------------
  # Row 6 (M1-6): Live Real-Agent 8-Task A/B (Gated behind SKILL_ARENA_REAL_AB=1)
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 6/7] Live 8-task real-agent A/B gate (SKILL_ARENA_REAL_AB)..."
  ROW6_PASSED=0
  ROW6_SKIPPED=0
  if [[ "${SKILL_ARENA_REAL_AB:-${RELAY_REAL_AGENTS:-0}}" == "1" ]]; then
    echo "  SKILL_ARENA_REAL_AB=1 enabled: running live real-agent A/B via real-ab-gate.py..."
    "$PYTHON" "${ROOT_DIR}/Scripts/acceptance/real-ab-gate.py" \
      --db "${TMP_WORK}/real_ab.sqlite" \
      --html-out "${TMP_WORK}/real_ab.html"
    ROW6_PASSED=1
  else
    echo "  SKIPPED (没跑): SKILL_ARENA_REAL_AB!=1; live paid 8-task real-agent A/B requires explicit user approval and API budget."
    ROW6_SKIPPED=1
  fi

  # ------------------------------------------------------------------
  # Row 7 (W1 + KR8): Scripted Live M1 Mutations on Isolated Clone
  # ------------------------------------------------------------------
  echo ""
  echo "[Row 7/7] Running 10 scripted M1 mutation checks on isolated repository clone (W1, KR2, KR4, KR6, KR8)..."
  MUT_REPO="${TMP_WORK}/mut_repo"
  git clone -q "$ROOT_DIR" "$MUT_REPO"
  restore_mut_repo() {
    if [[ -d "${MUT_REPO:-}/.git" ]]; then
      git -C "$MUT_REPO" checkout -- . >/dev/null 2>&1 || true
      git -C "$MUT_REPO" clean -fd >/dev/null 2>&1 || true
    fi
    cleanup
  }
  trap restore_mut_repo EXIT
  "$PYTHON" - "$ROOT_DIR" "$MUT_REPO" <<'PY'
import shutil, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
mut = Path(sys.argv[2])
for sub in ("src", "tests", "Scripts"):
    shutil.rmtree(mut / sub, ignore_errors=True)
    shutil.copytree(
        root / sub,
        mut / sub,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
for fname in ("README.md", "README.zh-CN.md", "pyproject.toml"):
    if (root / fname).is_file():
        shutil.copy2(root / fname, mut / fname)
subprocess.run(["git", "-C", str(mut), "config", "user.name", "MutCheck"], check=True)
subprocess.run(["git", "-C", str(mut), "config", "user.email", "mut@example.com"], check=True)
subprocess.run(["git", "-C", str(mut), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(mut), "commit", "--allow-empty", "-q", "-m", "working-tree-sync"], check=True)
PY

  run_pytest_in_mut_repo() {
    local test_rel="$1"
    local test_func="$2"
    "$PYTHON" - "$MUT_REPO" "${test_rel}::${test_func}" <<'PY'
import os, sys
mut_repo = sys.argv[1]
nodeid = sys.argv[2]
mut_src = os.path.join(mut_repo, "src")
sys.meta_path = [
    finder for finder in sys.meta_path
    if "editable" not in type(finder).__name__.lower()
    and "editable" not in type(finder).__module__.lower()
]
sys.path.insert(0, mut_src)
os.chdir(mut_repo)
import skill_arena
assert skill_arena.__file__ and os.path.realpath(skill_arena.__file__).startswith(os.path.realpath(mut_src)), skill_arena.__file__
import pytest
raise SystemExit(pytest.main(["-v", "--tb=short", nodeid]))
PY
  }

  verify_named_mutation() {
    local label="$1"
    local target_rel="$2"
    local test_rel="$3"
    local test_func="$4"
    local patch_code="$5"
    local nodeid="${test_rel}::${test_func}"

    "$PYTHON" - "${MUT_REPO}/${target_rel}" "$patch_code" <<'PY'
from pathlib import Path
import py_compile, sys
target_file = Path(sys.argv[1])
code = sys.argv[2]
ns = {"target_file": target_file, "src": target_file.read_text(encoding="utf-8")}
exec(code, ns)
py_compile.compile(str(target_file), doraise=True)
PY

    set +e
    MUT_OUT="$(run_pytest_in_mut_repo "$test_rel" "$test_func" 2>&1)"
    MUT_RC=$?
    set -e

    if [[ "$MUT_RC" -ne 1 ]]; then
      echo "FAIL: ${label} exited with code ${MUT_RC} (expected pytest failure exit code 1)!" >&2
      echo "$MUT_OUT" >&2
      exit 1
    fi
    if ! echo "$MUT_OUT" | grep -q "FAILED ${nodeid}"; then
      echo "FAIL: ${label} did not fail named test ${nodeid}!" >&2
      echo "$MUT_OUT" >&2
      exit 1
    fi

    git -C "$MUT_REPO" checkout -- .
    git -C "$MUT_REPO" clean -fd >/dev/null
    if [[ -n "$(git -C "$MUT_REPO" status --porcelain)" ]]; then
      echo "FAIL: ${label} left dirty git status in mutation clone after restore!" >&2
      exit 1
    fi

    if ! run_pytest_in_mut_repo "$test_rel" "$test_func" >/dev/null 2>&1; then
      echo "FAIL: ${label} named test ${nodeid} did not pass after restoring clean source!" >&2
      exit 1
    fi
    echo "  ${label} -> FAILED ${nodeid} (exit 1) & restored clean (git status empty, test GREEN)."
  }

  verify_named_mutation \
    "Mutation M1-1a (omit isolated HOME override)" \
    "src/skill_arena/sandbox/skill_injector.py" \
    "tests/test_sandbox.py" \
    "test_per_trial_isolated_home_control_zero_skills_treatment_only_candidate" \
    '
needle = "\"HOME\": home_str,"
assert needle in src, "M1-1a needle missing"
target_file.write_text(src.replace(needle, "", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-1b (leak global skill into Control arm HOME)" \
    "src/skill_arena/sandbox/skill_injector.py" \
    "tests/test_sandbox.py" \
    "test_per_trial_isolated_home_control_zero_skills_treatment_only_candidate" \
    '
needle = "            if auth_copy_mode or contender.auth_copy_mode:"
leak_line = "            (trial_home / \".agents/skills/leaked/SKILL.md\").parent.mkdir(parents=True, exist_ok=True); (trial_home / \".agents/skills/leaked/SKILL.md\").write_text(\"# leak\\n\", encoding=\"utf-8\")\n" + needle
assert needle in src, "M1-1b needle missing"
target_file.write_text(src.replace(needle, leak_line, 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-2 (fail to strip outer RELAY_WORKER before relay.sh)" \
    "src/skill_arena/runners/harness_driver.py" \
    "tests/test_sandbox.py" \
    "test_relay_sh_integration_parses_log_md_strips_relay_worker_and_cleans_workspace" \
    '
needle = "unset_keys = (\"RELAY_WORKER\",)"
assert needle in src, "M1-2 needle missing"
target_file.write_text(src.replace(needle, "unset_keys = ()", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-3 (report buffered total latency instead of streaming TTFT)" \
    "src/skill_arena/runners/xray_bridge.py" \
    "tests/test_proxy.py" \
    "test_xray_bridge_streaming_sse_ttft_within_50ms" \
    '
needle = "ttft_val = round(float(t[\"ttft_ms\"]), 3) if t.get(\"ttft_ms\") is not None else 0.0"
assert needle in src, "M1-3 needle missing"
target_file.write_text(src.replace(needle, "ttft_val = round(float(t.get(\"latency_ms\", 0)), 3)", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-4 (drop cached_tokens from OpenAI Responses usage)" \
    "src/skill_arena/runners/xray_bridge.py" \
    "tests/test_proxy.py" \
    "test_xray_bridge_openai_responses_api_streaming_and_json_usage" \
    '
needle = "in_cache_read = int(u.get(\"cache_read_tokens\", 0))"
assert needle in src, "M1-4 needle missing"
target_file.write_text(src.replace(needle, "in_cache_read = 0", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-5 (rank winner without Newcombe-Wilson CI spans-zero check)" \
    "src/skill_arena/store/arena_db.py" \
    "tests/test_store.py" \
    "test_newcombe_wilson_ci_20_vs_20_equal_rates_is_inconclusive_with_no_winner" \
    '
needle = "    elif ci_low <= 0.0 <= ci_high:"
assert needle in src, "M1-5 needle missing"
target_file.write_text(src.replace(needle, "    elif False and (ci_low <= 0.0 <= ci_high):", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-KR2a (disable cumulative --budget-usd pre-trial halt)" \
    "src/skill_arena/runners/harness_driver.py" \
    "tests/test_cli.py" \
    "test_kr2_experiment_wide_budget_ceiling_stops_before_exceeding_budget" \
    '
needle = "        remaining = float(self.budget_usd) - self.cumulative_cost_usd"
assert needle in src, "M1-KR2a needle missing"
target_file.write_text(src.replace(needle, "        return (True, float(self.budget_usd))", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-KR2b (return exit 0 instead of EXIT_BUDGET_EXCEEDED on budget halt)" \
    "src/skill_arena/cli.py" \
    "tests/test_cli.py" \
    "test_kr8_budget_halt_returns_dedicated_exit_code_and_discloses_first_trial_overrun" \
    '
needle = "        if bool(report.get(\"budget_stopped\")):\n            return EXIT_BUDGET_EXCEEDED"
assert needle in src, "M1-KR2b needle missing"
target_file.write_text(src.replace(needle, "        if bool(report.get(\"budget_stopped\")):\n            return 0", 1), encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-KR4 (bypass child env allowlist and inherit SSH_AUTH_SOCK / CLAUDE_CODE_*)" \
    "src/skill_arena/runners/harness_driver.py" \
    "tests/test_sandbox.py" \
    "test_kr4_strict_child_env_allowlist_blocks_ssh_claude_code_and_host_secrets" \
    '
needle_init = "    allowed: dict[str, str] = {}"
needle_strip = "    for forbidden in _FORBIDDEN_CHILD_ENV_KEYS:\n        allowed.pop(forbidden, None)\n    for key in [k for k in allowed if k.startswith(\"CLAUDE_CODE_\")]:\n        allowed.pop(key, None)"
assert needle_init in src and needle_strip in src, "M1-KR4 needle missing"
patched = src.replace(needle_init, "    allowed: dict[str, str] = dict(os.environ)", 1)
patched = patched.replace(needle_strip, "    pass", 1)
target_file.write_text(patched, encoding="utf-8")
'

  verify_named_mutation \
    "Mutation M1-KR6 (forward OPENAI_API_KEY and ANTHROPIC_API_KEY by default without --pass-env)" \
    "src/skill_arena/runners/harness_driver.py" \
    "tests/test_sandbox.py" \
    "test_kr6_zero_api_keys_forwarded_by_default_and_pass_env_forwards_only_named_vars" \
    '
needle = "    \"VIRTUAL_ENV\","
assert needle in src, "M1-KR6 needle missing"
target_file.write_text(src.replace(needle, "    \"VIRTUAL_ENV\",\n    \"OPENAI_API_KEY\",\n    \"ANTHROPIC_API_KEY\",", 1), encoding="utf-8")
'

  echo "  ROW 7 [PASS]: All 10 M1 mutations failed their exact named tests (exit 1) and restored cleanly."

  M1_PASSED=$((6 + ROW6_PASSED))
  echo ""
  echo "================================================================"
  echo "M1 ACCEPTANCE SUMMARY: ${M1_PASSED} passed, ${ROW6_SKIPPED} skipped"
  echo "RESULT: ALL M1 OFFLINE ACCEPTANCE CHECKS & 10 MUTATIONS PASSED (Row 6 live A/B: SKIPPED / 没跑)"
  echo "================================================================"
}

if [[ "$RECORD" -eq 1 ]]; then
  mkdir -p "${ROOT_DIR}/reports"
  RAW_OUT="${TMP_WORK}/raw_m1_acceptance.txt"
  run_suite 2>&1 | tee "$RAW_OUT"
  scrub_report_file "$RAW_OUT" "$REPORT_FILE"
else
  run_suite
fi
