"""Swiss Newsprint & Motorsport Telemetry single-file HTML compiler for SkillArena."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _clean_prose(text: str) -> str:
    """Sanitize em/en-dashes and mask local home-directory paths in exported HTML."""
    home = str(Path.home())
    cleaned = text.replace("\u2014", " - ").replace("\u2013", "-")
    if home and home != "/":
        cleaned = cleaned.replace(home, "~")
    return cleaned


def _fmt_pct(rate: float) -> str:
    return f"{rate * 100.0:.1f}%"


def _fmt_usd(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:.4f}"


def render_arena_html(report: dict[str, Any], default_view: str = "scoreboard") -> str:
    """Compile a self-contained zero-dependency HTML scoreboard and dual-track trajectory replay."""
    title = _clean_prose(str(report.get("title", "SkillArena Benchmark Report")))
    exp_id = str(report.get("experiment_id", "exp_local"))
    suite_name = str(report.get("suite_name", "builtin-8"))
    k_reps = int(report.get("k_reps", 3))
    leaderboard: list[dict[str, Any]] = list(report.get("leaderboard", []))
    task_matrix: list[dict[str, Any]] = list(report.get("task_matrix", []))

    control = next((r for r in leaderboard if r.get("role") == "control"), leaderboard[0] if leaderboard else {})
    treatment = next(
        (r for r in leaderboard if r.get("role") == "treatment"),
        leaderboard[1] if len(leaderboard) > 1 else control,
    )

    c_pass1 = float(control.get("pass_at_1", 0.0))
    t_pass1 = float(treatment.get("pass_at_1", 0.0))
    lift_pp = (t_pass1 - c_pass1) * 100.0
    lift_str = f"{lift_pp:+.1f} pp"

    c_cps = control.get("cost_per_success_usd")
    t_cps = treatment.get("cost_per_success_usd")
    overhead_tok = int(treatment.get("context_overhead_delta_tokens", 0))
    min_integrity = min((float(r.get("anti_cheat_integrity_rate", 1.0)) for r in leaderboard), default=1.0)

    # Server-rendered scoreboard rows for instant zero-JS readability & SEO/sloplint
    lb_rows_html: list[str] = []
    for row in leaderboard:
        is_treat = row.get("role") == "treatment"
        badge_cls = "arm-pill-treatment" if is_treat else "arm-pill-control"
        role_txt = "Treatment" if is_treat else "Control"
        skills_txt = ", ".join(row.get("skills_mounted") or []) or "None (stripped)"
        ovh = int(row.get("context_overhead_delta_tokens", 0))
        ovh_txt = f"+{ovh:,} tok" if ovh > 0 else (f"{ovh:,} tok" if ovh < 0 else "0 tok (baseline)")
        integrity_pct = float(row.get("anti_cheat_integrity_rate", 1.0))
        int_cls = "status-ok" if integrity_pct >= 0.999 else "status-fail"

        lb_rows_html.append(
            f"""
            <tr>
              <td>
                <div class="arm-cell">
                  <span class="arm-pill {badge_cls}">{html.escape(role_txt)}</span>
                  <div>
                    <div class="arm-name">{html.escape(_clean_prose(str(row.get("label", ""))))}</div>
                    <div class="arm-meta">{html.escape(str(row.get("cli", "")))} · {html.escape(str(row.get("model", "")))}</div>
                  </div>
                </div>
              </td>
              <td class="mono">{html.escape(skills_txt)}</td>
              <td class="mono strong">{_fmt_pct(float(row.get("pass_at_1", 0.0)))}</td>
              <td class="mono">{_fmt_pct(float(row.get("pass_hat_k", 0.0)))}</td>
              <td class="mono">{_fmt_usd(row.get("cost_per_success_usd"))}</td>
              <td class="mono">{ovh_txt}</td>
              <td class="mono">{float(row.get("median_latency_ms", 0)) / 1000.0:.2f}s</td>
              <td class="mono {int_cls}">{_fmt_pct(integrity_pct)}</td>
            </tr>
            """
        )

    c_id = str(control.get("contender_id", ""))
    t_id = str(treatment.get("contender_id", ""))

    matrix_rows_html: list[str] = []
    for idx, item in enumerate(task_matrix):
        tid = str(item["task_id"])
        ttitle = _clean_prose(str(item["title"]))
        tcat = str(item["category"])
        c_arm = item["arms"].get(c_id, {})
        t_arm = item["arms"].get(t_id, {})

        c_p = int(c_arm.get("passed_count", 0))
        c_tot = int(c_arm.get("total_reps", k_reps))
        t_p = int(t_arm.get("passed_count", 0))
        t_tot = int(t_arm.get("total_reps", k_reps))

        c_cls = "verdict-pass" if c_p == c_tot and c_tot > 0 else ("verdict-flaky" if c_p > 0 else "verdict-fail")
        t_cls = "verdict-pass" if t_p == t_tot and t_tot > 0 else ("verdict-flaky" if t_p > 0 else "verdict-fail")

        delta_val = t_p - c_p
        if delta_val > 0:
            delta_badge = f'<span class="delta-tag delta-win">+{delta_val}/{t_tot} gain</span>'
        elif delta_val < 0:
            delta_badge = f'<span class="delta-tag delta-reg">{delta_val}/{t_tot} regressed</span>'
        else:
            delta_badge = '<span class="delta-tag delta-tie">Parity</span>'

        matrix_rows_html.append(
            f"""
            <tr>
              <td class="mono task-id-col">{html.escape(tid)}</td>
              <td>
                <div class="task-title-text">{html.escape(ttitle)}</div>
                <div class="task-cat-text">Suite domain: {html.escape(tcat)}</div>
              </td>
              <td class="mono"><span class="verdict-pill {c_cls}">{c_p}/{c_tot} pass</span> · ${float(c_arm.get("mean_cost_usd", 0.0)):.4f}</td>
              <td class="mono"><span class="verdict-pill {t_cls}">{t_p}/{t_tot} pass</span> · ${float(t_arm.get("mean_cost_usd", 0.0)):.4f}</td>
              <td class="mono">{delta_badge}</td>
              <td>
                <button type="button" class="inspect-btn" data-replay-index="{idx}">Inspect dual replay</button>
              </td>
            </tr>
            """
        )

    safe_json = _clean_prose(json.dumps(report, ensure_ascii=True)).replace("</", "<\\/")
    score_active = "is-active" if default_view != "replay" else ""
    replay_active = "is-active" if default_view == "replay" else ""
    score_hidden = "hidden" if default_view == "replay" else ""
    replay_hidden = "" if default_view == "replay" else "hidden"

    return f"""<!doctype html>
<html lang="en" data-theme="newsprint">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="SkillArena controlled git-worktree A/B benchmark telemetry and dual-track trajectory replay.">
  <title>SkillArena Telemetry: {html.escape(title)}</title>
  <style>
    /* Hallmark · v2.0.0 | theme: swiss-newsprint-telemetry (custom) | genre: editorial | scope: app | signature: dual-track synchronized trajectory replay */
    /* critique: warm newsprint scoreboard paired with motorsport telemetry rules, tabular figures, and zero nested card chrome */
    :root {{
      --bg-page: #F9F7F1;
      --bg-surface: #FCFBF8;
      --bg-inset: #F1EFE7;
      --line-hairline: #DFDCD2;
      --line-strong: #C2BEB1;
      --ink-primary: #161514;
      --ink-secondary: #524F49;
      --ink-muted: #757067;
      --arm-control: #2D5B88;
      --arm-control-soft: rgba(45, 91, 136, 0.11);
      --arm-treatment: #176B47;
      --arm-treatment-soft: rgba(23, 107, 71, 0.12);
      --arm-regression: #C93B22;
      --arm-regression-soft: rgba(201, 59, 34, 0.12);
      --arm-amber: #9E5C11;
      --arm-amber-soft: rgba(158, 92, 17, 0.12);
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
      --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    }}

    html[data-theme="telemetry"] {{
      --bg-page: #151513;
      --bg-surface: #1E1E1B;
      --bg-inset: #262622;
      --line-hairline: #34332E;
      --line-strong: #4D4B44;
      --ink-primary: #F4F1EA;
      --ink-secondary: #B9B4A8;
      --ink-muted: #8C877B;
      --arm-control: #6BA3D6;
      --arm-control-soft: rgba(107, 163, 214, 0.16);
      --arm-treatment: #42B883;
      --arm-treatment-soft: rgba(66, 184, 131, 0.16);
      --arm-regression: #EE6B52;
      --arm-regression-soft: rgba(238, 107, 82, 0.16);
      --arm-amber: #E49C3E;
      --arm-amber-soft: rgba(228, 156, 62, 0.16);
    }}

    * {{ box-sizing: border-box; }}

    html, body {{
      margin: 0;
      padding: 0;
      overflow-x: clip;
      background: var(--bg-page);
      color: var(--ink-primary);
      font-family: var(--font-sans);
      font-size: 14px;
      line-height: 1.45;
      -webkit-font-smoothing: antialiased;
    }}

    h1, h2, h3 {{
      margin: 0;
      color: var(--ink-primary);
      text-wrap: balance;
      letter-spacing: -0.02em;
    }}

    .mono {{
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
    }}

    .strong {{ font-weight: 700; }}

    .topbar {{
      background: var(--bg-surface);
      border-bottom: 1px solid var(--line-hairline);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}

    .brand-cluster {{
      display: flex;
      align-items: baseline;
      gap: 12px;
    }}

    .brand-mark {{
      font-weight: 800;
      font-size: 17px;
      letter-spacing: -0.03em;
      color: var(--ink-primary);
    }}

    .brand-sub {{
      font-size: 12.5px;
      color: var(--ink-secondary);
    }}

    .nav-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .tab-btn, .ghost-btn, .inspect-btn {{
      appearance: none;
      border: 1px solid var(--line-hairline);
      background: var(--bg-inset);
      color: var(--ink-primary);
      padding: 8px 12px;
      font-size: 12.5px;
      font-weight: 600;
      border-radius: 6px;
      white-space: nowrap;
      cursor: pointer;
    }}

    .tab-btn:focus-visible, .ghost-btn:focus-visible, .inspect-btn:focus-visible, .replay-select:focus-visible, input:focus-visible {{
      outline: 2px solid var(--arm-control);
      outline-offset: 2px;
    }}

    .tab-btn:active, .ghost-btn:active, .inspect-btn:active {{
      transform: translateY(1px);
    }}

    .tab-btn:disabled, .ghost-btn:disabled, .inspect-btn:disabled {{
      opacity: 0.48;
      cursor: not-allowed;
    }}

    .tab-btn.is-active {{
      background: var(--ink-primary);
      color: var(--bg-surface);
      border-color: var(--ink-primary);
    }}

    .inspect-btn {{
      padding: 4px 12px;
      font-size: 12px;
      background: var(--bg-surface);
    }}

    .inspect-btn:hover, .ghost-btn:hover {{
      border-color: var(--line-strong);
    }}

    .page-shell {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}

    .exp-banner {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line-hairline);
    }}

    .exp-banner h1 {{
      font-size: 22px;
      font-weight: 750;
    }}

    .exp-meta-line {{
      margin-top: 4px;
      font-size: 12.5px;
      color: var(--ink-secondary);
    }}

    .kpi-strip {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      background: var(--bg-surface);
      border: 1px solid var(--line-hairline);
      border-radius: 8px;
    }}

    .kpi-cell {{
      padding: 16px 20px;
      border-right: 1px solid var(--line-hairline);
      color: var(--ink-primary);
    }}

    .kpi-cell:last-child {{ border-right: none; }}

    .kpi-label {{
      font-size: 12px;
      color: var(--ink-secondary);
      margin-bottom: 4px;
    }}

    .kpi-val {{
      font-size: 24px;
      font-weight: 750;
      letter-spacing: -0.03em;
    }}

    .kpi-val.tone-win {{ color: var(--arm-treatment); }}
    .kpi-val.tone-ctrl {{ color: var(--arm-control); }}
    .kpi-val.tone-amber {{ color: var(--arm-amber); }}

    .kpi-foot {{
      margin-top: 4px;
      font-size: 11.5px;
      color: var(--ink-muted);
    }}

    .panel {{
      background: var(--bg-surface);
      border: 1px solid var(--line-hairline);
      border-radius: 8px;
      color: var(--ink-primary);
      overflow: hidden;
    }}

    .panel-head {{
      padding: 12px 20px;
      border-bottom: 1px solid var(--line-hairline);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}

    .panel-head h2 {{
      font-size: 15px;
      font-weight: 700;
    }}

    .panel-note {{
      font-size: 12px;
      color: var(--ink-muted);
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}

    th, td {{
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--line-hairline);
      vertical-align: middle;
    }}

    th {{
      font-size: 12px;
      font-weight: 600;
      color: var(--ink-secondary);
      background: var(--bg-inset);
    }}

    tbody tr:last-child td {{ border-bottom: none; }}

    .arm-cell {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}

    .arm-pill {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11.5px;
      font-weight: 700;
    }}

    .arm-pill-control {{
      background: var(--arm-control-soft);
      color: var(--arm-control);
    }}

    .arm-pill-treatment {{
      background: var(--arm-treatment-soft);
      color: var(--arm-treatment);
    }}

    .arm-name {{ font-weight: 650; color: var(--ink-primary); }}
    .arm-meta {{ font-size: 11.5px; color: var(--ink-muted); }}

    .task-id-col {{ font-size: 12px; color: var(--ink-secondary); white-space: nowrap; }}
    .task-title-text {{ font-weight: 600; color: var(--ink-primary); }}
    .task-cat-text {{ font-size: 11.5px; color: var(--ink-muted); }}

    .verdict-pill, .delta-tag {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11.5px;
      font-weight: 650;
    }}

    .verdict-pass, .delta-win, .status-ok {{
      background: var(--arm-treatment-soft);
      color: var(--arm-treatment);
    }}

    .verdict-fail, .delta-reg, .status-fail {{
      background: var(--arm-regression-soft);
      color: var(--arm-regression);
    }}

    .verdict-flaky {{
      background: var(--arm-amber-soft);
      color: var(--arm-amber);
    }}

    .delta-tie {{
      background: var(--bg-inset);
      color: var(--ink-secondary);
    }}

    .replay-toolbar {{
      padding: 12px 20px;
      background: var(--bg-inset);
      border-bottom: 1px solid var(--line-hairline);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .replay-select {{
      padding: 8px 12px;
      border-radius: 6px;
      border: 1px solid var(--line-strong);
      background: var(--bg-surface);
      color: var(--ink-primary);
      font-size: 13px;
      font-weight: 600;
      min-width: 340px;
    }}

    .scrubber-group {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12.5px;
      color: var(--ink-secondary);
    }}

    .dual-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}

    .track-col {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      color: var(--ink-primary);
    }}

    .track-col:first-child {{
      border-right: 1px solid var(--line-hairline);
    }}

    .track-banner {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 12px;
      border-bottom: 2px solid var(--arm-control);
    }}

    .track-banner.is-treatment {{
      border-bottom-color: var(--arm-treatment);
    }}

    .step-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    .step-card {{
      border: 1px solid var(--line-hairline);
      background: var(--bg-inset);
      border-radius: 6px;
      padding: 12px;
      color: var(--ink-primary);
    }}

    .step-card.is-highlighted {{
      border-color: var(--ink-primary);
      background: var(--bg-surface);
      color: var(--ink-primary);
    }}

    .step-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 4px;
    }}

    .step-title {{ font-weight: 650; font-size: 13px; }}
    .step-metrics {{ font-size: 11.5px; color: var(--ink-muted); }}
    .step-detail {{ font-size: 12.5px; color: var(--ink-secondary); }}

    .diff-box {{
      margin: 0;
      padding: 12px;
      border-radius: 6px;
      border: 1px solid var(--line-hairline);
      background: var(--bg-inset);
      color: var(--ink-primary);
      font-size: 11.5px;
      line-height: 1.45;
      overflow-x: auto;
      max-height: 220px;
      white-space: pre;
    }}

    @media (max-width: 960px) {{
      .kpi-strip {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .dual-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand-cluster">
      <div class="brand-mark">SkillArena</div>
      <div class="brand-sub">Git-worktree A/B benchmark &amp; wire-tap token telemetry</div>
    </div>
    <nav class="nav-actions" aria-label="Workspace views">
      <button type="button" id="btn-view-scoreboard" class="tab-btn {score_active}">Scoreboard &amp; economics</button>
      <button type="button" id="btn-view-replay" class="tab-btn {replay_active}">Dual-track trajectory replay</button>
      <button type="button" id="btn-theme-toggle" class="ghost-btn">Switch telemetry theme</button>
    </nav>
  </header>

  <main class="page-shell">
    <section class="exp-banner">
      <div>
        <h1>{html.escape(title)}</h1>
        <div class="exp-meta-line mono">
          Experiment {html.escape(exp_id)} · Suite: {html.escape(suite_name)} ({len(task_matrix)} tasks x {k_reps} isolated worktree reps)
        </div>
      </div>
      <div class="mono panel-note">Zero-LLM deterministic unit + Hallmark craft + SHA-256 anti-cheat graders</div>
    </section>

    <section class="kpi-strip" aria-label="Headline experiment metrics">
      <div class="kpi-cell">
        <div class="kpi-label">Treatment Pass@1 lift</div>
        <div class="kpi-val mono tone-win">{html.escape(lift_str)}</div>
        <div class="kpi-foot mono">Control {_fmt_pct(c_pass1)} vs Treatment {_fmt_pct(t_pass1)}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">True cost per success</div>
        <div class="kpi-val mono tone-ctrl">{_fmt_usd(t_cps)}</div>
        <div class="kpi-foot mono">Control amortized: {_fmt_usd(c_cps)} (failed retries included)</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">Turn-1 skill context tax</div>
        <div class="kpi-val mono tone-amber">{overhead_tok:+,d} tok</div>
        <div class="kpi-foot mono">Prompt delta injected on initial request</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label">Anti-cheat test integrity</div>
        <div class="kpi-val mono">{_fmt_pct(min_integrity)}</div>
        <div class="kpi-foot mono">Protected test SHA-256 verified post-run</div>
      </div>
    </section>

    <div id="view-scoreboard" {score_hidden}>
      <section class="panel" style="margin-bottom: 20px;">
        <div class="panel-head">
          <h2>Contender A/B scoreboard</h2>
          <span class="panel-note mono">Pass^{k_reps} requires {k_reps}/{k_reps} consecutive clean passes per task</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Contender arm</th>
              <th>Mounted SKILL.md</th>
              <th>Pass@1</th>
              <th>Pass^{k_reps} consistency</th>
              <th>Cost / success</th>
              <th>Turn-1 context delta</th>
              <th>Median latency</th>
              <th>Test integrity</th>
            </tr>
          </thead>
          <tbody>
            {"".join(lb_rows_html)}
          </tbody>
        </table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Task-by-task worktree telemetry matrix</h2>
          <span class="panel-note">Select any row to open synchronized Control vs Treatment trajectory replay</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Task ID</th>
              <th>Engineering benchmark specification</th>
              <th>Control ({html.escape(_clean_prose(str(control.get("label", "Baseline"))))})</th>
              <th>Treatment ({html.escape(_clean_prose(str(treatment.get("label", "Skilled"))))})</th>
              <th>Outcome delta</th>
              <th>Trajectory</th>
            </tr>
          </thead>
          <tbody>
            {"".join(matrix_rows_html)}
          </tbody>
        </table>
      </section>
    </div>

    <section id="view-replay" class="panel" {replay_hidden}>
      <div class="replay-toolbar">
        <div>
          <label for="replay-task-select" class="panel-note" style="margin-right: 8px;">Benchmark task:</label>
          <select id="replay-task-select" class="replay-select"></select>
        </div>
        <div class="scrubber-group">
          <span>Synchronized step cursor:</span>
          <input type="range" id="step-scrubber" min="1" max="5" value="3">
          <span id="step-scrubber-label" class="mono strong">Step 3</span>
        </div>
      </div>
      <div class="dual-grid" id="dual-replay-stage"></div>
    </section>
  </main>

  <script id="arena-report-data" type="application/json">{safe_json}</script>
  <script>
    (function () {{
      const raw = document.getElementById("arena-report-data").textContent;
      const report = JSON.parse(raw);
      const leaderboard = report.leaderboard || [];
      const controlMeta = leaderboard.find(r => r.role === "control") || leaderboard[0] || {{}};
      const treatMeta = leaderboard.find(r => r.role === "treatment") || leaderboard[1] || controlMeta;
      const tasks = report.task_matrix || [];

      const btnScore = document.getElementById("btn-view-scoreboard");
      const btnReplay = document.getElementById("btn-view-replay");
      const viewScore = document.getElementById("view-scoreboard");
      const viewReplay = document.getElementById("view-replay");
      const selectEl = document.getElementById("replay-task-select");
      const scrubber = document.getElementById("step-scrubber");
      const scrubberLabel = document.getElementById("step-scrubber-label");
      const stage = document.getElementById("dual-replay-stage");

      function esc(str) {{
        return String(str || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;");
      }}

      function setView(mode) {{
        const isReplay = mode === "replay";
        viewScore.hidden = isReplay;
        viewReplay.hidden = !isReplay;
        btnScore.classList.toggle("is-active", !isReplay);
        btnReplay.classList.toggle("is-active", isReplay);
      }}

      tasks.forEach((t, i) => {{
        const opt = document.createElement("option");
        opt.value = String(i);
        opt.textContent = t.task_id + " : " + t.title;
        selectEl.appendChild(opt);
      }});

      function renderTrackColumn(armMeta, armData, activeStep, isTreatment) {{
        const trial = (armData && armData.rep1_trial) || null;
        if (!trial) {{
          return '<div class="track-col"><p>No trajectory recorded.</p></div>';
        }}
        const steps = trial.steps || [];
        const usage = trial.usage || {{}};
        const verdict = trial.verdict || {{}};
        const pillCls = trial.passed ? "verdict-pass" : "verdict-fail";
        const pillTxt = trial.passed ? "PASS (exit 0)" : "FAIL";
        const bannerCls = isTreatment ? "track-banner is-treatment" : "track-banner";

        const stepCards = steps.map((s, idx) => {{
          const hi = (idx + 1) === activeStep ? "step-card is-highlighted" : "step-card";
          const tokInfo = s.prompt_tokens
            ? (s.prompt_tokens + " in (" + s.cached_tokens + " cached) / " + s.completion_tokens + " out · $" + Number(s.cost_usd || 0).toFixed(4))
            : (s.latency_ms ? (s.latency_ms + " ms") : "workspace");
          return (
            '<div class="' + hi + '">' +
              '<div class="step-head">' +
                '<span class="step-title">' + idx_label(idx + 1, s.kind) + " " + esc(s.summary) + '</span>' +
                '<span class="step-metrics mono">' + esc(tokInfo) + '</span>' +
              '</div>' +
              '<div class="step-detail">' + esc(s.detail) + '</div>' +
            '</div>'
          );
        }}).join("");

        const diffText = (trial.git_diff || "(No implementation lines modified)").slice(0, 1800);
        return (
          '<div class="track-col">' +
            '<div class="' + bannerCls + '">' +
              '<div>' +
                '<div class="arm-name">' + esc(armMeta.label) + '</div>' +
                '<div class="arm-meta mono">' +
                  'Turn-1: ' + (usage.first_turn_prompt_tokens || 0) + ' tok · Total: $' + Number(usage.total_cost_usd || 0).toFixed(4) +
                  ' · Latency: ' + ((usage.total_latency_ms || 0) / 1000).toFixed(2) + 's' +
                '</div>' +
              '</div>' +
              '<span class="verdict-pill mono ' + pillCls + '">' + pillTxt + '</span>' +
            '</div>' +
            '<div class="step-list">' + stepCards + '</div>' +
            '<div>' +
              '<div class="panel-note mono" style="margin-bottom: 8px;">Grader summary: ' + esc(verdict.summary) + '</div>' +
              '<pre class="diff-box mono">' + esc(diffText) + '</pre>' +
            '</div>' +
          '</div>'
        );
      }}

      function idx_label(n, kind) {{
        return "[" + n + " · " + kind + "]";
      }}

      function renderReplay() {{
        const idx = Number(selectEl.value || 0);
        const task = tasks[idx];
        if (!task) return;
        const cArm = task.arms[controlMeta.contender_id] || {{}};
        const tArm = task.arms[treatMeta.contender_id] || {{}};
        const cSteps = (cArm.rep1_trial && cArm.rep1_trial.steps ? cArm.rep1_trial.steps.length : 1);
        const tSteps = (tArm.rep1_trial && tArm.rep1_trial.steps ? tArm.rep1_trial.steps.length : 1);
        const maxSteps = Math.max(cSteps, tSteps, 1);
        scrubber.max = String(maxSteps);
        if (Number(scrubber.value) > maxSteps) scrubber.value = String(maxSteps);
        const curStep = Number(scrubber.value || 1);
        scrubberLabel.textContent = "Step " + curStep + " / " + maxSteps;

        stage.innerHTML =
          renderTrackColumn(controlMeta, cArm, Math.min(curStep, cSteps), false) +
          renderTrackColumn(treatMeta, tArm, Math.min(curStep, tSteps), true);
      }}

      btnScore.addEventListener("click", () => setView("scoreboard"));
      btnReplay.addEventListener("click", () => {{ setView("replay"); renderReplay(); }});
      selectEl.addEventListener("change", renderReplay);
      scrubber.addEventListener("input", renderReplay);

      document.querySelectorAll("[data-replay-index]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          selectEl.value = btn.getAttribute("data-replay-index") || "0";
          setView("replay");
          renderReplay();
        }});
      }});

      document.getElementById("btn-theme-toggle").addEventListener("click", () => {{
        const root = document.documentElement;
        root.setAttribute(
          "data-theme",
          root.getAttribute("data-theme") === "telemetry" ? "newsprint" : "telemetry"
        );
      }});

      const params = new URLSearchParams(window.location.search);
      if (params.get("task")) {{
        const found = tasks.findIndex(t => t.task_id === params.get("task"));
        if (found >= 0) selectEl.value = String(found);
      }}
      renderReplay();
      if (params.get("view") === "replay") {{
        setView("replay");
      }}
    }})();
  </script>
</body>
</html>
"""
