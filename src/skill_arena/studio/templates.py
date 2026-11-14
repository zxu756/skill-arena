"""Swiss Newsprint & Motorsport Telemetry single-file bilingual HTML compiler for SkillArena."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from skill_arena.i18n import (
    CATEGORY_ZH,
    TASK_TITLE_ZH,
    localize_category,
    localize_task_title,
    resolve_html_lang_mode,
)


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


def _fmt_cost_cell(val: float | None, is_metered: bool, is_zh: bool) -> str:
    if not is_metered:
        return "未计量" if is_zh else "Unmetered"
    return _fmt_usd(val)


def _fmt_ovh_cell(val: int | None, is_metered: bool, is_zh: bool, *, kpi_mode: bool = False) -> str:
    if not is_metered or val is None:
        return "未计量" if is_zh else "Unmetered"
    iv = int(val)
    if kpi_mode:
        return f"{iv:+,d} tok"
    if iv > 0:
        return f"+{iv:,} tok"
    if iv < 0:
        return f"{iv:,} tok"
    return "0 tok（基线）" if is_zh else "0 tok (baseline)"


def _localize_title(raw_title: str, lang: str) -> str:
    if lang != "zh":
        return raw_title
    known = {
        "[SIMULATED] Synthetic Pipeline Calibration": "[SIMULATED / 模拟数据] 合成管线校准评测",
        "Control vs SKILL.md A/B Evaluation": "对照组 对比 SKILL.md 技能包 A/B 评测",
        "SkillArena Benchmark Report": "SkillArena 基准评测报告",
    }
    if raw_title in known:
        return known[raw_title]
    if raw_title.startswith("[SIMULATED] ") and "[SIMULATED / 模拟数据]" not in raw_title:
        return raw_title.replace("[SIMULATED] ", "[SIMULATED / 模拟数据] ", 1)
    return raw_title


def _localize_arm_label(label: str, lang: str) -> str:
    if lang != "zh":
        return label
    out = (
        label.replace("No Skill", "无技能基线")
        .replace("+ Synthetic Skill Pack", "+ 合成技能包")
    )
    if out.startswith("[SIMULATED] ") and "[SIMULATED / 模拟数据]" not in out:
        out = out.replace("[SIMULATED] ", "[SIMULATED / 模拟数据] ", 1)
    return out


def render_arena_html(
    report: dict[str, Any],
    default_view: str = "scoreboard",
    lang: str = "auto",
) -> str:
    """Compile a self-contained zero-dependency bilingual HTML scoreboard & trajectory replay."""
    render_lang, default_lang_attr = resolve_html_lang_mode(lang)
    is_zh = render_lang == "zh"
    html_lang_attr = "zh-CN" if is_zh else "en"

    raw_title = _clean_prose(str(report.get("title", "SkillArena Benchmark Report")))
    disp_title = _localize_title(raw_title, render_lang)
    exp_id = str(report.get("experiment_id", "exp_local"))
    suite_name = str(report.get("suite_name", "builtin-8"))
    k_reps = int(report.get("k_reps", 3))
    leaderboard: list[dict[str, Any]] = list(report.get("leaderboard", []))
    task_matrix: list[dict[str, Any]] = list(report.get("task_matrix", []))

    control = next(
        (r for r in leaderboard if r.get("role") == "control"),
        leaderboard[0] if leaderboard else {},
    )
    treatment = next(
        (r for r in leaderboard if r.get("role") == "treatment"),
        leaderboard[1] if len(leaderboard) > 1 else control,
    )

    c_pass1 = float(control.get("pass_at_1", 0.0))
    t_pass1 = float(treatment.get("pass_at_1", 0.0))
    lift_pp = (t_pass1 - c_pass1) * 100.0
    lift_str = f"{lift_pp:+.1f} pp"

    stat_comp: dict[str, Any] = dict(report.get("statistical_comparison") or {})
    stat_verdict = str(stat_comp.get("verdict", "inconclusive"))
    ci_low_pp = float(stat_comp.get("ci_low", 0.0)) * 100.0
    ci_high_pp = float(stat_comp.get("ci_high", 0.0)) * 100.0
    is_simulated_exp = bool(report.get("is_simulated"))
    if stat_verdict == "treatment_better":
        kpi1_tone_cls = "tone-win"
        kpi1_val_str = lift_str
        verdict_short = "实验组显著占优" if is_zh else "Treatment wins (95% CI > 0)"
    elif stat_verdict == "control_better":
        kpi1_tone_cls = "tone-ctrl"
        kpi1_val_str = lift_str
        verdict_short = "对照组显著占优" if is_zh else "Control wins (95% CI < 0)"
    else:
        kpi1_tone_cls = ""
        kpi1_val_str = "看不出差别" if is_zh else "Inconclusive"
        verdict_short = "看不出差别（不排定名次）" if is_zh else "Inconclusive (no rank)"
    if is_simulated_exp:
        verdict_short = f"[SIMULATED | 模拟] {verdict_short}" if is_zh else f"[SIMULATED] {verdict_short}"
    c_metered = bool(control.get("is_metered", True))
    t_metered = bool(treatment.get("is_metered", True))
    c_cps = control.get("cost_per_success_usd")
    t_cps = treatment.get("cost_per_success_usd")
    t_ovh = treatment.get("context_overhead_delta_tokens")
    kpi2_val_str = _fmt_cost_cell(t_cps, t_metered, is_zh)
    kpi3_val_str = _fmt_ovh_cell(t_ovh, t_metered, is_zh, kpi_mode=True)
    min_integrity = min(
        (float(r.get("anti_cheat_integrity_rate", 1.0)) for r in leaderboard),
        default=1.0,
    )

    # Server-rendered scoreboard rows for instant zero-JS readability & SEO/sloplint
    lb_rows_html: list[str] = []
    for row in leaderboard:
        is_treat = row.get("role") == "treatment"
        row_metered = bool(row.get("is_metered", True))
        badge_cls = "arm-pill-treatment" if is_treat else "arm-pill-control"
        role_txt = ("实验组" if is_treat else "对照组") if is_zh else ("Treatment" if is_treat else "Control")
        row_iso = bool(row.get("isolate_home", True))
        if row_iso:
            none_txt = "0 个技能（独立隔离 HOME）" if is_zh else "0 skills (isolated HOME)"
        else:
            none_txt = (
                "工作区未挂载（显式启用宿主 HOME）"
                if is_zh
                else "None in workspace (host HOME opted-in)"
            )
        skills_txt = ", ".join(row.get("skills_mounted") or []) or none_txt
        xray_spend_txt = _fmt_cost_cell(row.get("total_spend_usd"), row_metered, is_zh)
        relay_spend_txt = _fmt_usd(row.get("total_relay_cost_usd"))
        cps_txt = _fmt_cost_cell(row.get("cost_per_success_usd"), row_metered, is_zh)
        ovh_txt = _fmt_ovh_cell(row.get("context_overhead_delta_tokens"), row_metered, is_zh)
        elo_val = int(row.get("elo_rating", 1500))
        integrity_pct = float(row.get("anti_cheat_integrity_rate", 1.0))
        int_cls = "status-ok" if integrity_pct >= 0.999 else "status-fail"
        raw_arm_lbl = _localize_arm_label(_clean_prose(str(row.get("label", ""))), render_lang)
        row_rank = row.get("rank")
        arm_lbl = f"#{row_rank} {raw_arm_lbl}" if row_rank in (1, 2) else raw_arm_lbl

        lb_rows_html.append(
            f"""
            <tr>
              <td>
                <div class="arm-cell">
                  <span class="arm-pill {badge_cls}">{html.escape(role_txt)}</span>
                  <div>
                    <div class="arm-name">{html.escape(arm_lbl)}</div>
                    <div class="arm-meta">{html.escape(str(row.get("cli", "")))} · {html.escape(str(row.get("model", "")))}</div>
                  </div>
                </div>
              </td>
              <td class="mono">{html.escape(skills_txt)}</td>
              <td class="mono strong">{elo_val}</td>
              <td class="mono strong">{_fmt_pct(float(row.get("pass_at_1", 0.0)))}</td>
              <td class="mono">{_fmt_pct(float(row.get("pass_hat_k", 0.0)))}</td>
              <td class="mono">{html.escape(xray_spend_txt)}</td>
              <td class="mono">{html.escape(relay_spend_txt)}</td>
              <td class="mono">{html.escape(cps_txt)}</td>
              <td class="mono">{html.escape(ovh_txt)}</td>
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
        raw_ttitle = _clean_prose(str(item["title"]))
        ttitle = localize_task_title(tid, raw_ttitle, render_lang)
        tcat = str(item["category"])
        tcat_disp = localize_category(tcat, render_lang)
        c_arm = item["arms"].get(c_id, {})
        t_arm = item["arms"].get(t_id, {})

        c_p = int(c_arm.get("passed_count", 0))
        c_tot = int(c_arm.get("total_reps", k_reps))
        t_p = int(t_arm.get("passed_count", 0))
        t_tot = int(t_arm.get("total_reps", k_reps))

        c_cls = "verdict-pass" if c_p == c_tot and c_tot > 0 else ("verdict-flaky" if c_p > 0 else "verdict-fail")
        t_cls = "verdict-pass" if t_p == t_tot and t_tot > 0 else ("verdict-flaky" if t_p > 0 else "verdict-fail")

        pass_word = "通过" if is_zh else "pass"
        c_arm_metered = bool(c_arm.get("is_metered", True)) and c_arm.get("mean_cost_usd") is not None
        t_arm_metered = bool(t_arm.get("is_metered", True)) and t_arm.get("mean_cost_usd") is not None
        c_cost_str = f"${float(c_arm['mean_cost_usd']):.4f}" if c_arm_metered else ("未计量" if is_zh else "Unmetered")
        if c_arm.get("mean_relay_cost_usd") is not None:
            c_cost_str = f"{c_cost_str} · relay ${float(c_arm['mean_relay_cost_usd']):.4f}"
        t_cost_str = f"${float(t_arm['mean_cost_usd']):.4f}" if t_arm_metered else ("未计量" if is_zh else "Unmetered")
        if t_arm.get("mean_relay_cost_usd") is not None:
            t_cost_str = f"{t_cost_str} · relay ${float(t_arm['mean_relay_cost_usd']):.4f}"
        delta_val = t_p - c_p
        if delta_val > 0:
            gain_txt = f"+{delta_val}/{t_tot} 净提升" if is_zh else f"+{delta_val}/{t_tot} gain"
            delta_badge = f'<span class="delta-tag delta-win">{gain_txt}</span>'
        elif delta_val < 0:
            reg_txt = f"{delta_val}/{t_tot} 退化" if is_zh else f"{delta_val}/{t_tot} regressed"
            delta_badge = f'<span class="delta-tag delta-reg">{reg_txt}</span>'
        else:
            tie_txt = "持平" if is_zh else "Parity"
            delta_badge = f'<span class="delta-tag delta-tie">{tie_txt}</span>'

        domain_line = f"所属领域：{tcat_disp}" if is_zh else f"Suite domain: {tcat_disp}"
        btn_inspect = "查看双轨回放" if is_zh else "Inspect dual replay"

        matrix_rows_html.append(
            f"""
            <tr data-task-cat="{html.escape(tcat)}" data-task-gain="{1 if delta_val > 0 else 0}">
              <td class="mono task-id-col">{html.escape(tid)}</td>
              <td class="task-spec-col">
                <div class="task-title-text">{html.escape(ttitle)}</div>
                <div class="task-cat-text">{html.escape(domain_line)}</div>
              </td>
              <td class="mono"><span class="verdict-pill {c_cls}">{c_p}/{c_tot} {pass_word}</span> · {html.escape(c_cost_str)}</td>
              <td class="mono"><span class="verdict-pill {t_cls}">{t_p}/{t_tot} {pass_word}</span> · {html.escape(t_cost_str)}</td>
              <td class="mono">{delta_badge}</td>
              <td>
                <button type="button" class="inspect-btn" data-replay-index="{idx}">{btn_inspect}</button>
              </td>
            </tr>
            """
        )

    trial_ledger_rows_html: list[str] = []
    for tr in list(report.get("trials") or []):
        tr_u = dict(tr.get("usage") or {})
        tr_metered = bool(tr_u.get("is_metered", False))
        tr_xray = (
            f"${float(tr_u['total_cost_usd']):.4f}"
            if (tr_metered and tr_u.get("total_cost_usd") is not None)
            else ("未计量" if is_zh else "Unmetered")
        )
        tr_relay = (
            f"${float(tr_u['relay_cost_usd']):.4f}"
            if tr_u.get("relay_cost_usd") is not None
            else "n/a"
        )
        tr_role_raw = str(tr.get("contender_role", ""))
        tr_role_txt = ("实验组" if tr_role_raw == "treatment" else "对照组") if is_zh else ("Treatment" if tr_role_raw == "treatment" else "Control")
        tr_ok = bool(tr.get("passed"))
        tr_cls = "verdict-pass" if tr_ok else "verdict-fail"
        tr_res_txt = ("通过" if tr_ok else "失败") if is_zh else ("PASS" if tr_ok else "FAIL")
        trial_ledger_rows_html.append(
            f"""
            <tr>
              <td class="mono">{html.escape(str(tr.get("task_id", "")))}</td>
              <td class="mono">{html.escape(tr_role_txt)}</td>
              <td class="mono">{int(tr.get("rep_index", 1))}</td>
              <td class="mono"><span class="verdict-pill {tr_cls}">{html.escape(tr_res_txt)}</span></td>
              <td class="mono">{html.escape(tr_xray)}</td>
              <td class="mono">{html.escape(tr_relay)}</td>
            </tr>
            """
        )

    tl_heading = "逐试次代理与 Relay 费用台账" if is_zh else "Per-trial XRay & Relay cost ledger"
    tl_rep_hdr = "轮次" if is_zh else "Rep"
    tl_res_hdr = "结果" if is_zh else "Result"

    safe_json = _clean_prose(json.dumps(report, ensure_ascii=True)).replace("</", "<\\/")
    zh_titles_json = _clean_prose(json.dumps(TASK_TITLE_ZH, ensure_ascii=False)).replace("</", "<\\/")
    zh_cats_json = _clean_prose(json.dumps(CATEGORY_ZH, ensure_ascii=False)).replace("</", "<\\/")

    score_active = "is-active" if default_view != "replay" else ""
    replay_active = "is-active" if default_view == "replay" else ""
    score_hidden = "hidden" if default_view == "replay" else ""
    replay_hidden = "" if default_view == "replay" else "hidden"
    sim_banner_hidden = "" if is_simulated_exp else "hidden"
    sim_banner_txt = (
        "[SIMULATED / 模拟数据] 仅为合成管线校准演示数据 - 未调用任何真实模型或外部 SKILL.md。"
        if is_zh
        else "[SIMULATED] Synthetic calibration data only - no real model or external SKILL.md was executed."
    )
    budget_val = report.get("budget_usd")
    cum_cost_val = float(report.get("cumulative_cost_usd") or 0.0)
    budget_stopped = bool(report.get("budget_stopped"))
    planned_n = int(report.get("planned_trials") or 0)
    completed_n = int(report.get("completed_trials") or 0)
    budget_banner_hidden = "" if (budget_stopped and budget_val is not None) else "hidden"
    if budget_stopped and budget_val is not None:
        budget_banner_txt = (
            (
                f"[预算上限停止] 实验总预算上限 ${float(budget_val):.4f} · 累计花费 ${cum_cost_val:.4f} · "
                f"已完成 {completed_n}/{planned_n} 次试验（在超出预算前停止）。"
            )
            if is_zh
            else (
                f"[BUDGET STOPPED] Budget ceiling ${float(budget_val):.4f} · cumulative spend ${cum_cost_val:.4f} · "
                f"completed {completed_n}/{planned_n} planned trials (halted before exceeding budget)."
            )
        )
    else:
        budget_banner_txt = ""

    zh_active = "is-active" if is_zh else ""
    en_active = "" if is_zh else "is-active"
    zh_pressed = "true" if is_zh else "false"
    en_pressed = "false" if is_zh else "true"

    doc_title = (
        f"SkillArena 遥测看板：{disp_title}"
        if is_zh
        else f"SkillArena Telemetry: {disp_title}"
    )
    brand_sub = (
        "Git Worktree 隔离沙盒 A/B 评测与流量监听遥测"
        if is_zh
        else "Git-worktree A/B benchmark & wire-tap token telemetry"
    )
    tab_score_txt = "排行榜与成本效能" if is_zh else "Scoreboard & economics"
    tab_replay_txt = "双轨轨迹与补丁回放" if is_zh else "Dual-track trajectory replay"
    theme_btn_txt = "深色遥测主题" if is_zh else "Switch telemetry theme"
    budget_meta_suffix = ""
    if budget_val is not None:
        budget_meta_suffix = (
            f" · 总预算上限 ${float(budget_val):.4f}（累计 ${cum_cost_val:.4f}，{completed_n}/{planned_n} 次）"
            if is_zh
            else f" · Budget ceiling ${float(budget_val):.4f} (spent ${cum_cost_val:.4f}, {completed_n}/{planned_n} trials)"
        )
    exp_meta_txt = (
        f"实验编号 {exp_id} · 评测集：{suite_name}（{len(task_matrix)} 项任务 x {k_reps} 轮独立 Worktree 重复）{budget_meta_suffix}"
        if is_zh
        else f"Experiment {exp_id} · Suite: {suite_name} ({len(task_matrix)} tasks x {k_reps} isolated worktree reps){budget_meta_suffix}"
    )
    grader_note_txt = (
        "零大模型裁判 · 确定性单测 + Hallmark 工艺检查 + SHA-256 防篡改校验"
        if is_zh
        else "Zero-LLM deterministic unit + Hallmark craft + SHA-256 anti-cheat graders"
    )

    kpi1_lbl = "统计结论与 Pass@1 差值" if is_zh else "Statistical verdict & Pass@1 lift"
    kpi1_foot = (
        f"差值 {lift_pp:+.1f} pp · 95% 区间 [{ci_low_pp:+.1f}, {ci_high_pp:+.1f}] pp · {verdict_short}"
        if is_zh
        else f"Delta {lift_pp:+.1f} pp · 95% CI [{ci_low_pp:+.1f}, {ci_high_pp:+.1f}] pp · {verdict_short}"
    )
    c_cps_disp = _fmt_cost_cell(c_cps, c_metered, is_zh)
    kpi2_lbl = "单次成功真实摊销成本" if is_zh else "True cost per success"
    kpi2_foot = (
        f"对照组摊销：{c_cps_disp}（含失败重试损耗）"
        if is_zh
        else f"Control amortized: {c_cps_disp} (failed retries included)"
    )
    kpi3_lbl = "首轮上下文税" if is_zh else "Turn-1 Context Tax"
    kpi3_foot = (
        "实验组与对照组首轮输入 Token 均值之差"
        if is_zh
        else "Mean Turn-1 input token delta (treatment minus baseline)"
    )
    kpi4_lbl = "防作弊测试完整性" if is_zh else "Anti-cheat test integrity"
    kpi4_foot = (
        "受保护测试文件运行后 SHA-256 校验通过"
        if is_zh
        else "Protected test SHA-256 verified post-run"
    )

    sb_heading = "参赛智能体 A/B 排行榜" if is_zh else "Contender A/B scoreboard"
    sb_note = (
        f"结论：{verdict_short} · 成对胜率换算的 Elo（双组）与 Pass^{k_reps}（单任务需连续 {k_reps}/{k_reps} 次全绿通过）"
        if is_zh
        else f"Verdict: {verdict_short} · Win-Rate Derived Elo (2-arm) & Pass^{k_reps} ({k_reps}/{k_reps} consecutive clean passes per task)"
    )
    th_arm = "参赛组别" if is_zh else "Contender arm"
    th_skill = "挂载技能" if is_zh else "Mounted SKILL.md"
    th_elo = "胜率换算 Elo" if is_zh else "Win-Rate Elo (2-arm)"
    th_pass1 = "Pass@1"
    th_passk = f"Pass^{k_reps} 稳健率" if is_zh else f"Pass^{k_reps} consistency"
    th_xray = "代理计量花费" if is_zh else "XRay spend"
    th_relay = "Relay日志花费" if is_zh else "Relay LOG spend"
    th_cps = "单次成功成本" if is_zh else "Cost / success"
    th_delta = "首轮上下文税" if is_zh else "Turn-1 Context Tax"
    th_lat = "中位耗时" if is_zh else "Median latency"
    th_int = "防作弊完整性" if is_zh else "Test integrity"

    mat_heading = "逐任务 Worktree 沙盒遥测矩阵" if is_zh else "Task-by-task worktree telemetry matrix"
    f_all = "全部" if is_zh else "All"
    f_gains = "技能净胜" if is_zh else "Skill gains"
    f_conc = "并发控制" if is_zh else "Concurrency"
    f_ui = "前端工艺" if is_zh else "Frontend craft"
    f_refac = "重构测试" if is_zh else "Refactor"
    f_sec = "安全加固" if is_zh else "Security"

    ctrl_hdr_lbl = _localize_arm_label(_clean_prose(str(control.get("label", "Baseline"))), render_lang)
    treat_hdr_lbl = _localize_arm_label(_clean_prose(str(treatment.get("label", "Skilled"))), render_lang)
    mth_id = "任务 ID" if is_zh else "Task ID"
    mth_spec = "工程基准测试规范" if is_zh else "Engineering benchmark specification"
    mth_ctrl = f"对照组 ({ctrl_hdr_lbl})" if is_zh else f"Control ({ctrl_hdr_lbl})"
    mth_treat = f"实验组 ({treat_hdr_lbl})" if is_zh else f"Treatment ({treat_hdr_lbl})"
    mth_delta = "结果净变化" if is_zh else "Outcome delta"
    mth_traj = "双轨回放" if is_zh else "Trajectory"

    rep_task_lbl = "基准测试任务：" if is_zh else "Benchmark task:"
    rf_both = "轨迹与补丁" if is_zh else "Steps & diff"
    rf_steps = "仅执行轨迹" if is_zh else "Steps only"
    rf_diff = "仅代码补丁" if is_zh else "Worktree diff"
    rep_cursor_lbl = "同步步骤游标：" if is_zh else "Synchronized step cursor:"
    rep_step_init = "第 3 步" if is_zh else "Step 3"

    return f"""<!doctype html>
<html lang="{html_lang_attr}" data-theme="newsprint" data-default-lang="{default_lang_attr}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="SkillArena controlled git-worktree A/B benchmark telemetry and dual-track trajectory replay.">
  <title>{html.escape(doc_title)}</title>
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
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
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
      overflow-wrap: break-word;
      min-width: 0;
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
      flex-wrap: wrap;
    }}

    .brand-cluster {{
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
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
      flex-wrap: wrap;
    }}

    .tab-btn, .ghost-btn, .inspect-btn {{
      appearance: none;
      border: 1px solid var(--line-hairline);
      background: var(--bg-inset);
      color: var(--ink-primary);
      padding: 8px 12px;
      font-family: var(--font-sans);
      font-size: 12.5px;
      font-weight: 600;
      border-radius: 6px;
      white-space: nowrap;
      cursor: pointer;
    }}

    .lang-switch {{
      display: inline-flex;
      align-items: center;
      padding: 4px;
      background: var(--bg-inset);
      border-radius: 999px;
      gap: 4px;
    }}

    .lang-btn {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--ink-secondary);
      padding: 4px 12px;
      font-family: var(--font-sans);
      font-size: 12px;
      font-weight: 650;
      border-radius: 999px;
      white-space: nowrap;
      cursor: pointer;
    }}

    .lang-sep {{
      color: var(--ink-muted);
      font-size: 11px;
      user-select: none;
    }}

    .filter-bar {{
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
    }}

    .filter-pill {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--ink-secondary);
      padding: 4px 8px;
      font-family: var(--font-sans);
      font-size: 12px;
      font-weight: 600;
      border-radius: 4px;
      white-space: nowrap;
      cursor: pointer;
    }}

    .tab-btn:focus-visible, .ghost-btn:focus-visible, .inspect-btn:focus-visible,
    .filter-pill:focus-visible, .lang-btn:focus-visible, .replay-select:focus-visible, input:focus-visible {{
      outline: 2px solid var(--arm-control);
      outline-offset: 2px;
    }}

    .tab-btn:active, .ghost-btn:active, .inspect-btn:active, .filter-pill:active, .lang-btn:active {{
      transform: translateY(1px);
    }}

    .tab-btn:disabled, .ghost-btn:disabled, .inspect-btn:disabled, .filter-pill:disabled, .lang-btn:disabled {{
      opacity: 0.48;
      cursor: not-allowed;
    }}

    .tab-btn.is-active {{
      background: var(--ink-primary);
      color: var(--bg-surface);
      border-color: var(--ink-primary);
    }}

    .lang-btn.is-active {{
      background: var(--ink-primary);
      color: var(--bg-surface);
    }}

    .filter-pill.is-active {{
      background: var(--bg-inset);
      color: var(--ink-primary);
    }}

    .inspect-btn {{
      padding: 4px 12px;
      font-size: 12px;
      background: var(--bg-surface);
    }}

    .inspect-btn:hover, .ghost-btn:hover {{
      border-color: var(--line-strong);
    }}

    .filter-pill:hover, .lang-btn:hover {{
      color: var(--ink-primary);
    }}

    .lang-btn.is-active:hover {{
      color: var(--bg-surface);
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
      flex-wrap: wrap;
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
      min-width: 0;
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
      flex-wrap: wrap;
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
      white-space: nowrap;
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
      white-space: nowrap;
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
      padding: 8px 32px 8px 12px;
      border-radius: 6px;
      border: 1px solid var(--line-strong);
      background: var(--bg-surface);
      color: var(--ink-primary);
      font-family: var(--font-sans);
      font-size: 13px;
      font-weight: 600;
      min-width: 320px;
      max-width: 760px;
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
      min-width: 0;
    }}

    .track-col:first-child {{
      border-right: 1px solid var(--line-hairline);
    }}

    .track-banner {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
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
    .step-metrics {{ font-size: 11.5px; color: var(--ink-muted); white-space: nowrap; }}
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

    .sim-banner {{
      padding: 12px 16px;
      border-radius: 6px;
      border: 1px solid var(--arm-amber);
      background: var(--arm-amber-soft);
      color: var(--arm-amber);
      font-weight: 700;
      font-size: 13px;
    }}

    @media (max-width: 960px) {{
      .kpi-strip {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .dual-grid {{ grid-template-columns: 1fr; }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      .tab-btn, .ghost-btn, .inspect-btn, .filter-pill, .lang-btn {{
        transform: none !important;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand-cluster">
      <div class="brand-mark">SkillArena</div>
      <div class="brand-sub" id="brand-sub">{html.escape(brand_sub)}</div>
    </div>
    <nav class="nav-actions" aria-label="Workspace views">
      <button type="button" id="btn-view-scoreboard" class="tab-btn {score_active}">{html.escape(tab_score_txt)}</button>
      <button type="button" id="btn-view-replay" class="tab-btn {replay_active}">{html.escape(tab_replay_txt)}</button>
      <div class="lang-switch" id="lang-switcher" role="group" aria-label="中文 / EN language switcher">
        <button type="button" class="lang-btn {zh_active}" data-lang-switch="zh" aria-pressed="{zh_pressed}">中文</button>
        <span class="lang-sep" aria-hidden="true">|</span>
        <button type="button" class="lang-btn {en_active}" data-lang-switch="en" aria-pressed="{en_pressed}">EN</button>
      </div>
      <button type="button" id="btn-theme-toggle" class="ghost-btn">{html.escape(theme_btn_txt)}</button>
    </nav>
  </header>

  <main class="page-shell">
    <section class="sim-banner mono" id="sim-banner" {sim_banner_hidden}>{html.escape(sim_banner_txt)}</section>
    <section class="sim-banner mono" id="budget-banner" {budget_banner_hidden}>{html.escape(budget_banner_txt)}</section>

    <section class="exp-banner">
      <div>
        <h1 id="exp-title">{html.escape(disp_title)}</h1>
        <div class="exp-meta-line mono" id="exp-meta-line">{html.escape(exp_meta_txt)}</div>
      </div>
      <div class="mono panel-note" id="grader-note">{html.escape(grader_note_txt)}</div>
    </section>

    <section class="kpi-strip" aria-label="Headline experiment metrics">
      <div class="kpi-cell">
        <div class="kpi-label" id="kpi1-lbl">{html.escape(kpi1_lbl)}</div>
        <div class="kpi-val mono {kpi1_tone_cls}" id="kpi1-val">{html.escape(kpi1_val_str)}</div>
        <div class="kpi-foot mono" id="kpi1-foot">{html.escape(kpi1_foot)}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label" id="kpi2-lbl">{html.escape(kpi2_lbl)}</div>
        <div class="kpi-val mono tone-ctrl" id="kpi2-val">{html.escape(kpi2_val_str)}</div>
        <div class="kpi-foot mono" id="kpi2-foot">{html.escape(kpi2_foot)}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label" id="kpi3-lbl">{html.escape(kpi3_lbl)}</div>
        <div class="kpi-val mono tone-amber" id="kpi3-val">{html.escape(kpi3_val_str)}</div>
        <div class="kpi-foot mono" id="kpi3-foot">{html.escape(kpi3_foot)}</div>
      </div>
      <div class="kpi-cell">
        <div class="kpi-label" id="kpi4-lbl">{html.escape(kpi4_lbl)}</div>
        <div class="kpi-val mono" id="kpi4-val">{_fmt_pct(min_integrity)}</div>
        <div class="kpi-foot mono" id="kpi4-foot">{html.escape(kpi4_foot)}</div>
      </div>
    </section>

    <div id="view-scoreboard" {score_hidden}>
      <section class="panel" style="margin-bottom: 20px;">
        <div class="panel-head">
          <h2 id="sb-heading">{html.escape(sb_heading)}</h2>
          <span class="panel-note mono" id="sb-note">{html.escape(sb_note)}</span>
        </div>
        <table>
          <thead>
            <tr>
              <th id="th-arm">{html.escape(th_arm)}</th>
              <th id="th-skill">{html.escape(th_skill)}</th>
              <th id="th-elo">{html.escape(th_elo)}</th>
              <th id="th-pass1">{html.escape(th_pass1)}</th>
              <th id="th-passk">{html.escape(th_passk)}</th>
              <th id="th-xray">{html.escape(th_xray)}</th>
              <th id="th-relay">{html.escape(th_relay)}</th>
              <th id="th-cps">{html.escape(th_cps)}</th>
              <th id="th-delta">{html.escape(th_delta)}</th>
              <th id="th-lat">{html.escape(th_lat)}</th>
              <th id="th-int">{html.escape(th_int)}</th>
            </tr>
          </thead>
          <tbody id="lb-tbody">
            {"".join(lb_rows_html)}
          </tbody>
        </table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 id="mat-heading">{html.escape(mat_heading)}</h2>
          <div class="filter-bar" role="group" aria-label="Filter benchmark tasks">
            <button type="button" class="filter-pill is-active" data-task-filter="all">{html.escape(f_all)}</button>
            <button type="button" class="filter-pill" data-task-filter="gains">{html.escape(f_gains)}</button>
            <button type="button" class="filter-pill" data-task-filter="concurrency">{html.escape(f_conc)}</button>
            <button type="button" class="filter-pill" data-task-filter="frontend_craft">{html.escape(f_ui)}</button>
            <button type="button" class="filter-pill" data-task-filter="refactor_test">{html.escape(f_refac)}</button>
            <button type="button" class="filter-pill" data-task-filter="security_hygiene">{html.escape(f_sec)}</button>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th id="mth-id">{html.escape(mth_id)}</th>
              <th id="mth-spec">{html.escape(mth_spec)}</th>
              <th id="mth-ctrl">{html.escape(mth_ctrl)}</th>
              <th id="mth-treat">{html.escape(mth_treat)}</th>
              <th id="mth-delta">{html.escape(mth_delta)}</th>
              <th id="mth-traj">{html.escape(mth_traj)}</th>
            </tr>
          </thead>
          <tbody id="matrix-tbody">
            {"".join(matrix_rows_html)}
          </tbody>
        </table>
      </section>

      <section class="panel" style="margin-top: 20px;">
        <div class="panel-head">
          <h2 id="tl-heading">{html.escape(tl_heading)}</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th id="tl-th-task">{html.escape(mth_id)}</th>
              <th id="tl-th-arm">{html.escape(th_arm)}</th>
              <th id="tl-th-rep">{html.escape(tl_rep_hdr)}</th>
              <th id="tl-th-res">{html.escape(tl_res_hdr)}</th>
              <th id="tl-th-xray">{html.escape(th_xray)}</th>
              <th id="tl-th-relay">{html.escape(th_relay)}</th>
            </tr>
          </thead>
          <tbody id="trial-ledger-tbody">
            {"".join(trial_ledger_rows_html)}
          </tbody>
        </table>
      </section>
    </div>

    <section id="view-replay" class="panel" {replay_hidden}>
      <div class="replay-toolbar">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <label for="replay-task-select" class="panel-note" id="rep-task-lbl">{html.escape(rep_task_lbl)}</label>
          <select id="replay-task-select" class="replay-select"></select>
        </div>
        <div class="filter-bar" role="group" aria-label="Replay detail mode">
          <button type="button" class="filter-pill is-active" data-replay-mode="both" id="rf-both">{html.escape(rf_both)}</button>
          <button type="button" class="filter-pill" data-replay-mode="steps" id="rf-steps">{html.escape(rf_steps)}</button>
          <button type="button" class="filter-pill" data-replay-mode="diff" id="rf-diff">{html.escape(rf_diff)}</button>
        </div>
        <div class="scrubber-group">
          <span id="rep-cursor-lbl">{html.escape(rep_cursor_lbl)}</span>
          <input type="range" id="step-scrubber" min="1" max="5" value="3">
          <span id="step-scrubber-label" class="mono strong">{html.escape(rep_step_init)}</span>
        </div>
      </div>
      <div class="dual-grid" id="dual-replay-stage"></div>
    </section>
  </main>

  <script id="arena-report-data" type="application/json">{safe_json}</script>
  <script id="arena-zh-titles" type="application/json">{zh_titles_json}</script>
  <script id="arena-zh-cats" type="application/json">{zh_cats_json}</script>
  <script>
    (function () {{
      const report = JSON.parse(document.getElementById("arena-report-data").textContent);
      const ZH_TITLES = JSON.parse(document.getElementById("arena-zh-titles").textContent);
      const ZH_CATS = JSON.parse(document.getElementById("arena-zh-cats").textContent);

      const leaderboard = report.leaderboard || [];
      const controlMeta = leaderboard.find(r => r.role === "control") || leaderboard[0] || {{}};
      const treatMeta = leaderboard.find(r => r.role === "treatment") || leaderboard[1] || controlMeta;
      const tasks = report.task_matrix || [];
      const kReps = Number(report.k_reps || 3);

      const btnScore = document.getElementById("btn-view-scoreboard");
      const btnReplay = document.getElementById("btn-view-replay");
      const btnTheme = document.getElementById("btn-theme-toggle");
      const viewScore = document.getElementById("view-scoreboard");
      const viewReplay = document.getElementById("view-replay");
      const selectEl = document.getElementById("replay-task-select");
      const scrubber = document.getElementById("step-scrubber");
      const scrubberLabel = document.getElementById("step-scrubber-label");
      const stage = document.getElementById("dual-replay-stage");

      let currentLang = "en";
      let currentTaskFilter = "all";
      let currentReplayMode = "both";

      const I18N = {{
        en: {{
          docTitlePrefix: "SkillArena Telemetry: ",
          brandSub: "Git-worktree A/B benchmark & wire-tap token telemetry",
          simBanner: "[SIMULATED] Synthetic calibration data only - no real model or external SKILL.md was executed.",
          tabScore: "Scoreboard & economics",
          tabReplay: "Dual-track trajectory replay",
          themeToDark: "Switch telemetry theme",
          themeToLight: "Light newsprint theme",
          graderNote: "Zero-LLM deterministic unit + Hallmark craft + SHA-256 anti-cheat graders",
          kpi1Lbl: "Statistical verdict & Pass@1 lift",
          kpi2Lbl: "True cost per success",
          kpi3Lbl: "Turn-1 Context Tax",
          kpi3Foot: "Mean Turn-1 input token delta (treatment minus baseline)",
          kpi4Lbl: "Anti-cheat test integrity",
          kpi4Foot: "Protected test SHA-256 verified post-run",
          sbHeading: "Contender A/B scoreboard",
          thArm: "Contender arm",
          thSkill: "Mounted SKILL.md",
          thElo: "Win-Rate Elo (2-arm)",
          thPass1: "Pass@1",
          thXray: "XRay spend",
          thRelay: "Relay LOG spend",
          thCps: "Cost / success",
          thDelta: "Turn-1 Context Tax",
          thLat: "Median latency",
          thInt: "Test integrity",
          tlHeading: "Per-trial XRay & Relay cost ledger",
          tlRep: "Rep",
          tlRes: "Result",
          relayCostLbl: "Relay: ",
          roleControl: "Control",
          roleTreatment: "Treatment",
          noneIsolated: "0 skills (isolated HOME)",
          noneHostOptIn: "None in workspace (host HOME opted-in)",
          inconclusiveWord: "Inconclusive",
          inconclusiveShort: "Inconclusive (no rank)",
          treatWinsShort: "Treatment wins (95% CI > 0)",
          ctrlWinsShort: "Control wins (95% CI < 0)",
          zeroBaseline: "0 tok (baseline)",
          unmetered: "Unmetered",
          matHeading: "Task-by-task worktree telemetry matrix",
          fAll: "All",
          fGains: "Skill gains",
          fConc: "Concurrency",
          fUi: "Frontend craft",
          fRefac: "Refactor",
          fSec: "Security",
          mthId: "Task ID",
          mthSpec: "Engineering benchmark specification",
          mthDelta: "Outcome delta",
          mthTraj: "Trajectory",
          suiteDomain: "Suite domain: ",
          passWord: "pass",
          gainWord: "gain",
          regWord: "regressed",
          parity: "Parity",
          inspectBtn: "Inspect dual replay",
          emptyFilter: "No benchmark tasks match the selected filter.",
          repTaskLbl: "Benchmark task:",
          rfBoth: "Steps & diff",
          rfSteps: "Steps only",
          rfDiff: "Worktree diff",
          repCursorLbl: "Synchronized step cursor:",
          noTraj: "No trajectory recorded.",
          passExit0: "PASS (exit 0)",
          failBadge: "FAIL",
          turn1Lbl: "Turn-1: ",
          totalCostLbl: "Total: ",
          latLbl: "Latency: ",
          graderSum: "Grader summary: ",
          noDiff: "(No implementation lines modified)",
          workspaceTok: "workspace",
          kindMap: {{
            skill_mount: "skill_mount",
            thought: "thought",
            tool_call: "tool_call",
            file_patch: "file_patch",
            verify: "verify"
          }}
        }},
        zh: {{
          docTitlePrefix: "SkillArena 遥测看板：",
          brandSub: "Git Worktree 隔离沙盒 A/B 评测与流量监听遥测",
          simBanner: "[SIMULATED / 模拟数据] 仅为合成管线校准演示数据 - 未调用任何真实模型或外部 SKILL.md。",
          tabScore: "排行榜与成本效能",
          tabReplay: "双轨轨迹与补丁回放",
          themeToDark: "深色遥测主题",
          themeToLight: "浅色报刊主题",
          graderNote: "零大模型裁判 · 确定性单测 + Hallmark 工艺检查 + SHA-256 防篡改校验",
          kpi1Lbl: "统计结论与 Pass@1 差值",
          kpi2Lbl: "单次成功真实摊销成本",
          kpi3Lbl: "首轮上下文税",
          kpi3Foot: "实验组与对照组首轮输入 Token 均值之差",
          kpi4Lbl: "防作弊测试完整性",
          kpi4Foot: "受保护测试文件运行后 SHA-256 校验通过",
          sbHeading: "参赛智能体 A/B 排行榜",
          thArm: "参赛组别",
          thSkill: "挂载技能",
          thElo: "胜率换算 Elo",
          thPass1: "Pass@1",
          thXray: "代理计量花费",
          thRelay: "Relay日志花费",
          thCps: "单次成功成本",
          thDelta: "首轮上下文税",
          thLat: "中位耗时",
          thInt: "防作弊完整性",
          tlHeading: "逐试次代理与 Relay 费用台账",
          tlRep: "轮次",
          tlRes: "结果",
          relayCostLbl: "Relay：",
          roleControl: "对照组",
          roleTreatment: "实验组",
          noneIsolated: "0 个技能（独立隔离 HOME）",
          noneHostOptIn: "工作区未挂载（显式启用宿主 HOME）",
          inconclusiveWord: "看不出差别",
          inconclusiveShort: "看不出差别（不排定名次）",
          treatWinsShort: "实验组显著占优",
          ctrlWinsShort: "对照组显著占优",
          zeroBaseline: "0 tok（基线）",
          unmetered: "未计量",
          matHeading: "逐任务 Worktree 沙盒遥测矩阵",
          fAll: "全部",
          fGains: "技能净胜",
          fConc: "并发控制",
          fUi: "前端工艺",
          fRefac: "重构测试",
          fSec: "安全加固",
          mthId: "任务 ID",
          mthSpec: "工程基准测试规范",
          mthDelta: "结果净变化",
          mthTraj: "双轨回放",
          suiteDomain: "所属领域：",
          passWord: "通过",
          gainWord: "净提升",
          regWord: "退化",
          parity: "持平",
          inspectBtn: "查看双轨回放",
          emptyFilter: "当前筛选条件下暂无匹配任务。",
          repTaskLbl: "基准测试任务：",
          rfBoth: "轨迹与补丁",
          rfSteps: "仅执行轨迹",
          rfDiff: "仅代码补丁",
          repCursorLbl: "同步步骤游标：",
          noTraj: "未记录执行轨迹。",
          passExit0: "通过 (exit 0)",
          failBadge: "失败",
          turn1Lbl: "首轮：",
          totalCostLbl: "总计：",
          latLbl: "耗时：",
          graderSum: "判分摘要：",
          noDiff: "（未修改任何实现代码行）",
          workspaceTok: "沙盒环境",
          kindMap: {{
            skill_mount: "技能挂载",
            thought: "推理分析",
            tool_call: "工具调用",
            file_patch: "代码补丁",
            verify: "判分校验"
          }}
        }}
      }};

      function esc(str) {{
        return String(str || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;");
      }}

      function fmtPct(rate) {{
        return (Number(rate || 0) * 100).toFixed(1) + "%";
      }}

      function fmtUsd(val) {{
        return val === null || val === undefined ? "n/a" : ("$" + Number(val).toFixed(4));
      }}

      function fmtCostCell(val, isMetered) {{
        const t = I18N[currentLang];
        if (isMetered === false) return t.unmetered;
        return fmtUsd(val);
      }}

      function fmtOvhCell(val, isMetered, kpiMode) {{
        const t = I18N[currentLang];
        if (isMetered === false || val === null || val === undefined) return t.unmetered;
        const iv = Number(val);
        if (kpiMode) {{
          return (iv >= 0 ? "+" : "") + iv.toLocaleString("en-US") + " tok";
        }}
        if (iv > 0) return "+" + iv.toLocaleString("en-US") + " tok";
        if (iv < 0) return iv.toLocaleString("en-US") + " tok";
        return t.zeroBaseline;
      }}

      function localizeExpTitle(raw) {{
        if (currentLang !== "zh") return raw;
        const map = {{
          "[SIMULATED] Synthetic Pipeline Calibration": "[SIMULATED / 模拟数据] 合成管线校准评测",
          "Control vs SKILL.md A/B Evaluation": "对照组 对比 SKILL.md 技能包 A/B 评测",
          "SkillArena Benchmark Report": "SkillArena 基准评测报告"
        }};
        if (map[raw]) return map[raw];
        const s = String(raw || "");
        if (s.startsWith("[SIMULATED] ") && !s.includes("[SIMULATED / 模拟数据]")) {{
          return s.replace("[SIMULATED] ", "[SIMULATED / 模拟数据] ");
        }}
        return s;
      }}

      function localizeArmLabel(lbl) {{
        const s = String(lbl || "");
        if (currentLang !== "zh") return s;
        let out = s
          .replace("No Skill", "无技能基线")
          .replace("+ Synthetic Skill Pack", "+ 合成技能包");
        if (out.startsWith("[SIMULATED] ") && !out.includes("[SIMULATED / 模拟数据]")) {{
          out = out.replace("[SIMULATED] ", "[SIMULATED / 模拟数据] ");
        }}
        return out;
      }}

      function localizeTaskTitle(task) {{
        if (currentLang === "zh" && ZH_TITLES[task.task_id]) {{
          return ZH_TITLES[task.task_id];
        }}
        return task.title;
      }}

      function localizeCat(cat) {{
        if (currentLang === "zh" && ZH_CATS[cat]) {{
          return ZH_CATS[cat];
        }}
        return cat;
      }}

      function localizeStepSummary(s) {{
        const raw = String(s || "");
        if (currentLang !== "zh") return raw;
        let prefix = "";
        let txt = raw;
        if (txt.startsWith("[SIMULATED] ")) {{
          prefix = "[SIMULATED / 模拟数据] ";
          txt = txt.slice("[SIMULATED] ".length);
        }}
        if (txt.startsWith("Loaded synthetic profile ")) return prefix + txt.replace("Loaded synthetic profile ", "已加载合成配置 ");
        if (txt.startsWith("Loaded SKILL.md ")) return prefix + txt.replace("Loaded SKILL.md ", "已挂载 SKILL.md ");
        if (txt === "Analyzed task invariants & SKILL.md guardrails") return prefix + "分析任务不变量与 SKILL.md 约束护栏";
        if (txt === "Scanned prompt without specialized skill checklist") return prefix + "在无专项技能清单下扫描任务提示词";
        if (txt === "Inspected source files & ran exploratory command") return prefix + "检查源码文件并运行探索命令";
        if (txt === "Wrote partial implementation attempt") return prefix + "编写局部修复补丁尝试";
        if (txt === "Retried after test failure without root-cause fix") return prefix + "单测失败后重试但未定位根因";
        if (txt === "Deterministic Grader -> PASS") return prefix + "确定性判分器 -> 通过";
        if (txt === "Deterministic Grader -> FAIL") return prefix + "确定性判分器 -> 失败";
        const mRead = txt.match(/^Read workspace & inspected (\\d+) files$/);
        if (mRead) return prefix + "读取沙盒工作区并检查 " + mRead[1] + " 个文件";
        const mPatch = txt.match(/^Applied targeted patch \\((\\d+) file\\)$/);
        if (mPatch) return prefix + "应用精准修复补丁（" + mPatch[1] + " 个文件）";
        return prefix + txt;
      }}

      function localizeStepDetail(d) {{
        const txt = String(d || "");
        if (currentLang !== "zh") return txt;
        if (txt === "Identified exact edge-case contract before editing implementation.") return "修改代码前已确认并发、边界与排版契约。";
        if (txt === "Drafted surface patch without checking concurrency/sloplint edge cases.") return "仅生成表层补丁，未核查并发竞态与 sloplint 边界规范。";
        if (txt === "Missed strict invariant / left off-grid CSS or unguarded lock path.") return "遗漏关键不变量，残留非 4px 栅格样式或未加锁路径。";
        if (txt === "Burned extra completion tokens re-reading stack trace.") return "反复读取报错堆栈，额外消耗大量输出 Token。";
        const mOvh = txt.match(/^Turn-1 context overhead: \\+(\\d+) tokens mounted into \\.agents\\/skills\\/\\.$/);
        if (mOvh) return "首轮上下文增量：向 .agents/skills/ 注入 +" + mOvh[1] + " tokens。";
        const mChk = txt.match(/^Checked (.+) and test suite\\.$/);
        if (mChk) return "已核对 " + mChk[1] + " 与受保护单元测试套件。";
        const mOpen = txt.match(/^Opened (.+)\\.$/);
        if (mOpen) return "已打开 " + mOpen[1] + "。";
        const mUpd = txt.match(/^Updated (.+) with minimal diff\\.$/);
        if (mUpd) return "以最小代码变更更新 " + mUpd[1] + "。";
        return localizeVerdictSummary(txt);
      }}

      function localizeVerdictSummary(v) {{
        const txt = String(v || "");
        if (currentLang !== "zh") return txt;
        return txt
          .replace("Unit tests passed (exit 0)", "单元测试通过 (exit 0)")
          .replace(/Tests exited with code (\\d+)/, "测试进程退出码 $1（未通过）")
          .replace(/diff \\+(\\d+)\\/-(\\d+) \\(([\\d.]+)x budget\\)/, "代码变更 +$1/-$2 行（占预算 $3x）")
          .replace("Hallmark craft clean", "Hallmark 工艺零告警")
          .replace(/Hallmark craft FAIL=(\\d+), WARN=(\\d+)/, "Hallmark 工艺违规 FAIL=$1, WARN=$2");
      }}

      function syncThemeButtonLabel() {{
        const t = I18N[currentLang];
        const isDark = document.documentElement.getAttribute("data-theme") === "telemetry";
        btnTheme.textContent = isDark ? t.themeToLight : t.themeToDark;
      }}

      function setView(mode) {{
        const isReplay = mode === "replay";
        viewScore.hidden = isReplay;
        viewReplay.hidden = !isReplay;
        btnScore.classList.toggle("is-active", !isReplay);
        btnReplay.classList.toggle("is-active", isReplay);
      }}

      function populateTaskSelect() {{
        const prev = selectEl.value || "0";
        selectEl.innerHTML = "";
        tasks.forEach((t, i) => {{
          const opt = document.createElement("option");
          opt.value = String(i);
          opt.textContent = t.task_id + " : " + localizeTaskTitle(t);
          selectEl.appendChild(opt);
        }});
        selectEl.value = prev;
      }}

      function renderScoreboardAndMatrix() {{
        const t = I18N[currentLang];
        const simBannerEl = document.getElementById("sim-banner");
        if (simBannerEl) {{
          simBannerEl.hidden = !report.is_simulated;
          simBannerEl.textContent = t.simBanner;
        }}
        const budgetBannerEl = document.getElementById("budget-banner");
        const hasBudget = report.budget_usd !== null && report.budget_usd !== undefined;
        const bCeiling = Number(report.budget_usd || 0).toFixed(4);
        const bCum = Number(report.cumulative_cost_usd || 0).toFixed(4);
        const bDone = Number(report.completed_trials || 0);
        const bPlan = Number(report.planned_trials || 0);
        if (budgetBannerEl) {{
          const showBudgetBanner = Boolean(report.budget_stopped && hasBudget);
          budgetBannerEl.hidden = !showBudgetBanner;
          budgetBannerEl.textContent = showBudgetBanner
            ? (currentLang === "zh"
                ? ("[预算上限停止] 实验总预算上限 $" + bCeiling + " · 累计花费 $" + bCum + " · 已完成 " + bDone + "/" + bPlan + " 次试验（在超出预算前停止）。")
                : ("[BUDGET STOPPED] Budget ceiling $" + bCeiling + " · cumulative spend $" + bCum + " · completed " + bDone + "/" + bPlan + " planned trials (halted before exceeding budget)."))
            : "";
        }}
        const expTitle = localizeExpTitle(report.title || "SkillArena Benchmark Report");
        document.title = t.docTitlePrefix + expTitle;
        document.getElementById("brand-sub").textContent = t.brandSub;
        btnScore.textContent = t.tabScore;
        btnReplay.textContent = t.tabReplay;
        syncThemeButtonLabel();

        const bMetaSuffix = hasBudget
          ? (currentLang === "zh"
              ? (" · 总预算上限 $" + bCeiling + "（累计 $" + bCum + "，" + bDone + "/" + bPlan + " 次）")
              : (" · Budget ceiling $" + bCeiling + " (spent $" + bCum + ", " + bDone + "/" + bPlan + " trials)"))
          : "";
        document.getElementById("exp-title").textContent = expTitle;
        document.getElementById("exp-meta-line").textContent =
          currentLang === "zh"
            ? ("实验编号 " + report.experiment_id + " · 评测集：" + report.suite_name + "（" + tasks.length + " 项任务 x " + kReps + " 轮独立 Worktree 重复）" + bMetaSuffix)
            : ("Experiment " + report.experiment_id + " · Suite: " + report.suite_name + " (" + tasks.length + " tasks x " + kReps + " isolated worktree reps)" + bMetaSuffix);
        document.getElementById("grader-note").textContent = t.graderNote;

        const cPass1 = Number(controlMeta.pass_at_1 || 0);
        const tPass1 = Number(treatMeta.pass_at_1 || 0);
        const liftPp = (tPass1 - cPass1) * 100;
        const liftStr = (liftPp >= 0 ? "+" : "") + liftPp.toFixed(1) + " pp";
        const sc = report.statistical_comparison || {{}};
        const sVerdict = sc.verdict || "inconclusive";
        const ciLowPp = (Number(sc.ci_low || 0) * 100);
        const ciHighPp = (Number(sc.ci_high || 0) * 100);
        const ciRangeTxt = "[" + (ciLowPp >= 0 ? "+" : "") + ciLowPp.toFixed(1) + ", " + (ciHighPp >= 0 ? "+" : "") + ciHighPp.toFixed(1) + "] pp";
        const kpi1ValEl = document.getElementById("kpi1-val");
        kpi1ValEl.classList.remove("tone-win", "tone-ctrl");
        let verdictShort = t.inconclusiveShort;
        if (sVerdict === "treatment_better") {{
          kpi1ValEl.classList.add("tone-win");
          kpi1ValEl.textContent = liftStr;
          verdictShort = t.treatWinsShort;
        }} else if (sVerdict === "control_better") {{
          kpi1ValEl.classList.add("tone-ctrl");
          kpi1ValEl.textContent = liftStr;
          verdictShort = t.ctrlWinsShort;
        }} else {{
          kpi1ValEl.textContent = t.inconclusiveWord;
        }}
        if (report.is_simulated) {{
          verdictShort = (currentLang === "zh" ? "[SIMULATED | 模拟] " : "[SIMULATED] ") + verdictShort;
        }}
        document.getElementById("kpi1-lbl").textContent = t.kpi1Lbl;
        document.getElementById("kpi1-foot").textContent =
          currentLang === "zh"
            ? ("差值 " + liftStr + " · 95% 区间 " + ciRangeTxt + " · " + verdictShort)
            : ("Delta " + liftStr + " · 95% CI " + ciRangeTxt + " · " + verdictShort);

        const cMetered = controlMeta.is_metered !== false;
        const tMetered = treatMeta.is_metered !== false;
        document.getElementById("kpi2-lbl").textContent = t.kpi2Lbl;
        document.getElementById("kpi2-val").textContent = fmtCostCell(treatMeta.cost_per_success_usd, tMetered);
        document.getElementById("kpi2-foot").textContent =
          currentLang === "zh"
            ? ("对照组摊销：" + fmtCostCell(controlMeta.cost_per_success_usd, cMetered) + "（含失败重试损耗）")
            : ("Control amortized: " + fmtCostCell(controlMeta.cost_per_success_usd, cMetered) + " (failed retries included)");

        document.getElementById("kpi3-lbl").textContent = t.kpi3Lbl;
        document.getElementById("kpi3-val").textContent = fmtOvhCell(treatMeta.context_overhead_delta_tokens, tMetered, true);
        document.getElementById("kpi3-foot").textContent = t.kpi3Foot;
        document.getElementById("kpi4-lbl").textContent = t.kpi4Lbl;
        document.getElementById("kpi4-foot").textContent = t.kpi4Foot;

        document.getElementById("sb-heading").textContent = t.sbHeading;
        document.getElementById("sb-note").textContent =
          currentLang === "zh"
            ? ("结论：" + verdictShort + " · 成对胜率换算的 Elo（双组）与 Pass^" + kReps + "（单任务需连续 " + kReps + "/" + kReps + " 次全绿通过）")
            : ("Verdict: " + verdictShort + " · Win-Rate Derived Elo (2-arm) & Pass^" + kReps + " (" + kReps + "/" + kReps + " consecutive clean passes per task)");

        document.getElementById("th-arm").textContent = t.thArm;
        document.getElementById("th-skill").textContent = t.thSkill;
        document.getElementById("th-elo").textContent = t.thElo;
        document.getElementById("th-pass1").textContent = t.thPass1;
        document.getElementById("th-passk").textContent =
          currentLang === "zh" ? ("Pass^" + kReps + " 稳健率") : ("Pass^" + kReps + " consistency");
        document.getElementById("th-xray").textContent = t.thXray;
        document.getElementById("th-relay").textContent = t.thRelay;
        document.getElementById("th-cps").textContent = t.thCps;
        document.getElementById("th-delta").textContent = t.thDelta;
        document.getElementById("th-lat").textContent = t.thLat;
        document.getElementById("th-int").textContent = t.thInt;

        document.getElementById("lb-tbody").innerHTML = leaderboard.map((row) => {{
          const isTreat = row.role === "treatment";
          const rowMetered = row.is_metered !== false;
          const badgeCls = isTreat ? "arm-pill-treatment" : "arm-pill-control";
          const roleTxt = isTreat ? t.roleTreatment : t.roleControl;
          const rowIso = row.isolate_home !== false;
          const skillsTxt = (row.skills_mounted && row.skills_mounted.length)
            ? row.skills_mounted.join(", ")
            : (rowIso ? t.noneIsolated : t.noneHostOptIn);
          const xraySpendTxt = fmtCostCell(row.total_spend_usd, rowMetered);
          const relaySpendTxt = fmtUsd(row.total_relay_cost_usd);
          const cpsTxt = fmtCostCell(row.cost_per_success_usd, rowMetered);
          const ovhTxt = fmtOvhCell(row.context_overhead_delta_tokens, rowMetered, false);
          const eloVal = Number(row.elo_rating || 1500);
          const intPct = Number(row.anti_cheat_integrity_rate ?? 1.0);
          const intCls = intPct >= 0.999 ? "status-ok" : "status-fail";
          const dispLbl = (row.rank === 1 || row.rank === 2)
            ? ("#" + row.rank + " " + localizeArmLabel(row.label))
            : localizeArmLabel(row.label);
          return (
            "<tr>" +
              "<td><div class='arm-cell'>" +
                "<span class='arm-pill " + badgeCls + "'>" + esc(roleTxt) + "</span>" +
                "<div>" +
                  "<div class='arm-name'>" + esc(dispLbl) + "</div>" +
                  "<div class='arm-meta'>" + esc(row.cli) + " · " + esc(row.model) + "</div>" +
                "</div>" +
              "</div></td>" +
              "<td class='mono'>" + esc(skillsTxt) + "</td>" +
              "<td class='mono strong'>" + eloVal + "</td>" +
              "<td class='mono strong'>" + fmtPct(row.pass_at_1) + "</td>" +
              "<td class='mono'>" + fmtPct(row.pass_hat_k) + "</td>" +
              "<td class='mono'>" + esc(xraySpendTxt) + "</td>" +
              "<td class='mono'>" + esc(relaySpendTxt) + "</td>" +
              "<td class='mono'>" + esc(cpsTxt) + "</td>" +
              "<td class='mono'>" + esc(ovhTxt) + "</td>" +
              "<td class='mono'>" + (Number(row.median_latency_ms || 0) / 1000).toFixed(2) + "s</td>" +
              "<td class='mono " + intCls + "'>" + fmtPct(intPct) + "</td>" +
            "</tr>"
          );
        }}).join("");

        document.getElementById("mat-heading").textContent = t.matHeading;
        const filterMap = {{
          all: t.fAll,
          gains: t.fGains,
          concurrency: t.fConc,
          frontend_craft: t.fUi,
          refactor_test: t.fRefac,
          security_hygiene: t.fSec
        }};
        document.querySelectorAll("[data-task-filter]").forEach((btn) => {{
          const key = btn.getAttribute("data-task-filter");
          if (filterMap[key]) btn.textContent = filterMap[key];
          btn.classList.toggle("is-active", key === currentTaskFilter);
        }});

        document.getElementById("mth-id").textContent = t.mthId;
        document.getElementById("mth-spec").textContent = t.mthSpec;
        document.getElementById("mth-ctrl").textContent =
          t.roleControl + " (" + localizeArmLabel(controlMeta.label || "Baseline") + ")";
        document.getElementById("mth-treat").textContent =
          t.roleTreatment + " (" + localizeArmLabel(treatMeta.label || "Skilled") + ")";
        document.getElementById("mth-delta").textContent = t.mthDelta;
        document.getElementById("mth-traj").textContent = t.mthTraj;

        const cId = controlMeta.contender_id || "";
        const tId = treatMeta.contender_id || "";
        const filteredRows = [];
        tasks.forEach((item, idx) => {{
          const cArm = (item.arms && item.arms[cId]) || {{}};
          const tArm = (item.arms && item.arms[tId]) || {{}};
          const cP = Number(cArm.passed_count || 0);
          const cTot = Number(cArm.total_reps || kReps);
          const tP = Number(tArm.passed_count || 0);
          const tTot = Number(tArm.total_reps || kReps);
          const deltaVal = tP - cP;

          if (currentTaskFilter === "gains" && deltaVal <= 0) return;
          if (
            currentTaskFilter !== "all" &&
            currentTaskFilter !== "gains" &&
            item.category !== currentTaskFilter
          ) {{
            return;
          }}

          const cCls = (cP === cTot && cTot > 0) ? "verdict-pass" : (cP > 0 ? "verdict-flaky" : "verdict-fail");
          const tCls = (tP === tTot && tTot > 0) ? "verdict-pass" : (tP > 0 ? "verdict-flaky" : "verdict-fail");
          let cCostStr = (cArm.is_metered !== false && cArm.mean_cost_usd !== null && cArm.mean_cost_usd !== undefined)
            ? ("$" + Number(cArm.mean_cost_usd).toFixed(4))
            : t.unmetered;
          if (cArm.mean_relay_cost_usd !== null && cArm.mean_relay_cost_usd !== undefined) {{
            cCostStr += " · relay $" + Number(cArm.mean_relay_cost_usd).toFixed(4);
          }}
          let tCostStr = (tArm.is_metered !== false && tArm.mean_cost_usd !== null && tArm.mean_cost_usd !== undefined)
            ? ("$" + Number(tArm.mean_cost_usd).toFixed(4))
            : t.unmetered;
          if (tArm.mean_relay_cost_usd !== null && tArm.mean_relay_cost_usd !== undefined) {{
            tCostStr += " · relay $" + Number(tArm.mean_relay_cost_usd).toFixed(4);
          }}
          let deltaBadge = "<span class='delta-tag delta-tie'>" + esc(t.parity) + "</span>";
          if (deltaVal > 0) {{
            deltaBadge = "<span class='delta-tag delta-win'>+" + deltaVal + "/" + tTot + " " + esc(t.gainWord) + "</span>";
          }} else if (deltaVal < 0) {{
            deltaBadge = "<span class='delta-tag delta-reg'>" + deltaVal + "/" + tTot + " " + esc(t.regWord) + "</span>";
          }}

          filteredRows.push(
            "<tr>" +
              "<td class='mono task-id-col'>" + esc(item.task_id) + "</td>" +
              "<td class='task-spec-col'>" +
                "<div class='task-title-text'>" + esc(localizeTaskTitle(item)) + "</div>" +
                "<div class='task-cat-text'>" + esc(t.suiteDomain + localizeCat(item.category)) + "</div>" +
              "</td>" +
              "<td class='mono'><span class='verdict-pill " + cCls + "'>" + cP + "/" + cTot + " " + esc(t.passWord) + "</span> · " + esc(cCostStr) + "</td>" +
              "<td class='mono'><span class='verdict-pill " + tCls + "'>" + tP + "/" + tTot + " " + esc(t.passWord) + "</span> · " + esc(tCostStr) + "</td>" +
              "<td class='mono'>" + deltaBadge + "</td>" +
              "<td><button type='button' class='inspect-btn' data-replay-index='" + idx + "'>" + esc(t.inspectBtn) + "</button></td>" +
            "</tr>"
          );
        }});

        const matTbody = document.getElementById("matrix-tbody");
        if (!filteredRows.length) {{
          matTbody.innerHTML = "<tr><td colspan='6' class='panel-note'>" + esc(t.emptyFilter) + "</td></tr>";
        }} else {{
          matTbody.innerHTML = filteredRows.join("");
        }}

        matTbody.querySelectorAll("[data-replay-index]").forEach((btn) => {{
          btn.addEventListener("click", () => {{
            selectEl.value = btn.getAttribute("data-replay-index") || "0";
            setView("replay");
            renderReplay();
          }});
        }});

        document.getElementById("tl-heading").textContent = t.tlHeading;
        document.getElementById("tl-th-task").textContent = t.mthId;
        document.getElementById("tl-th-arm").textContent = t.thArm;
        document.getElementById("tl-th-rep").textContent = t.tlRep;
        document.getElementById("tl-th-res").textContent = t.tlRes;
        document.getElementById("tl-th-xray").textContent = t.thXray;
        document.getElementById("tl-th-relay").textContent = t.thRelay;
        const allTrials = report.trials || [];
        document.getElementById("trial-ledger-tbody").innerHTML = allTrials.map((tr) => {{
          const u = tr.usage || {{}};
          const uMet = u.is_metered !== false && u.total_cost_usd !== null && u.total_cost_usd !== undefined;
          const xStr = uMet ? ("$" + Number(u.total_cost_usd).toFixed(4)) : t.unmetered;
          const rStr = fmtUsd(u.relay_cost_usd);
          const roleLbl = tr.contender_role === "treatment" ? t.roleTreatment : t.roleControl;
          const ok = Boolean(tr.passed);
          const pCls = ok ? "verdict-pass" : "verdict-fail";
          const pTxt = ok ? (currentLang === "zh" ? "通过" : "PASS") : t.failBadge;
          return (
            "<tr>" +
              "<td class='mono'>" + esc(tr.task_id) + "</td>" +
              "<td class='mono'>" + esc(roleLbl) + "</td>" +
              "<td class='mono'>" + Number(tr.rep_index || 1) + "</td>" +
              "<td class='mono'><span class='verdict-pill " + pCls + "'>" + esc(pTxt) + "</span></td>" +
              "<td class='mono'>" + esc(xStr) + "</td>" +
              "<td class='mono'>" + esc(rStr) + "</td>" +
            "</tr>"
          );
        }}).join("");
      }}

      function idxLabel(n, kind) {{
        const t = I18N[currentLang];
        const kLabel = (t.kindMap && t.kindMap[kind]) || kind;
        return "[" + n + " · " + kLabel + "]";
      }}

      function renderTrackColumn(armMeta, armData, activeStep, isTreatment) {{
        const t = I18N[currentLang];
        const trial = (armData && armData.rep1_trial) || null;
        if (!trial) {{
          return '<div class="track-col"><p>' + esc(t.noTraj) + '</p></div>';
        }}
        const steps = trial.steps || [];
        const usage = trial.usage || {{}};
        const verdict = trial.verdict || {{}};
        const pillCls = trial.passed ? "verdict-pass" : "verdict-fail";
        const pillTxt = trial.passed ? t.passExit0 : t.failBadge;
        const bannerCls = isTreatment ? "track-banner is-treatment" : "track-banner";
        const uMetered = usage.is_metered !== false && usage.total_cost_usd !== null && usage.total_cost_usd !== undefined;
        const turn1Meta = (uMetered && usage.first_turn_prompt_tokens !== null && usage.first_turn_prompt_tokens !== undefined)
          ? (usage.first_turn_prompt_tokens + " tok")
          : t.unmetered;
        const totalCostMeta = uMetered
          ? ("$" + Number(usage.total_cost_usd).toFixed(4))
          : t.unmetered;
        const relayCostMeta = fmtUsd(usage.relay_cost_usd);

        const stepCards = steps.map((s, idx) => {{
          const hi = (idx + 1) === activeStep ? "step-card is-highlighted" : "step-card";
          let tokInfo = t.workspaceTok;
          if (s.prompt_tokens) {{
            tokInfo = currentLang === "zh"
              ? ("输入 " + s.prompt_tokens + " (缓存 " + s.cached_tokens + ") / 输出 " + s.completion_tokens + " · $" + Number(s.cost_usd || 0).toFixed(4))
              : (s.prompt_tokens + " in (" + s.cached_tokens + " cached) / " + s.completion_tokens + " out · $" + Number(s.cost_usd || 0).toFixed(4));
          }} else if (s.latency_ms) {{
            tokInfo = s.latency_ms + " ms";
          }}
          return (
            '<div class="' + hi + '">' +
              '<div class="step-head">' +
                '<span class="step-title">' + esc(idxLabel(idx + 1, s.kind)) + " " + esc(localizeStepSummary(s.summary)) + '</span>' +
                '<span class="step-metrics mono">' + esc(tokInfo) + '</span>' +
              '</div>' +
              '<div class="step-detail">' + esc(localizeStepDetail(s.detail)) + '</div>' +
            '</div>'
          );
        }}).join("");

        const diffText = (trial.git_diff || t.noDiff).slice(0, 1800);
        const stepsSection = currentReplayMode === "diff"
          ? ""
          : ('<div class="step-list">' + stepCards + '</div>');
        const diffSection = currentReplayMode === "steps"
          ? ""
          : (
            '<div>' +
              '<div class="panel-note mono" style="margin-bottom: 8px;">' + esc(t.graderSum + localizeVerdictSummary(verdict.summary)) + '</div>' +
              '<pre class="diff-box mono">' + esc(diffText) + '</pre>' +
            '</div>'
          );

        return (
          '<div class="track-col">' +
            '<div class="' + bannerCls + '">' +
              '<div>' +
                '<div class="arm-name">' + esc(localizeArmLabel(armMeta.label)) + '</div>' +
                '<div class="arm-meta mono">' +
                  esc(t.turn1Lbl + turn1Meta + " · " + t.totalCostLbl + totalCostMeta + " · " + t.relayCostLbl + relayCostMeta + " · " + t.latLbl) +
                  ((usage.total_latency_ms || 0) / 1000).toFixed(2) + "s" +
                '</div>' +
              '</div>' +
              '<span class="verdict-pill mono ' + pillCls + '">' + esc(pillTxt) + '</span>' +
            '</div>' +
            stepsSection +
            diffSection +
          '</div>'
        );
      }}

      function renderReplay() {{
        const t = I18N[currentLang];
        document.getElementById("rep-task-lbl").textContent = t.repTaskLbl;
        document.getElementById("rf-both").textContent = t.rfBoth;
        document.getElementById("rf-steps").textContent = t.rfSteps;
        document.getElementById("rf-diff").textContent = t.rfDiff;
        document.getElementById("rep-cursor-lbl").textContent = t.repCursorLbl;

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
        scrubberLabel.textContent = currentLang === "zh"
          ? ("第 " + curStep + " / " + maxSteps + " 步")
          : ("Step " + curStep + " / " + maxSteps);

        stage.innerHTML =
          renderTrackColumn(controlMeta, cArm, Math.min(curStep, cSteps), false) +
          renderTrackColumn(treatMeta, tArm, Math.min(curStep, tSteps), true);
      }}

      function setLang(lang, persist) {{
        currentLang = lang === "zh" ? "zh" : "en";
        document.documentElement.lang = currentLang === "zh" ? "zh-CN" : "en";
        if (persist) {{
          try {{ localStorage.setItem("skill-arena-lang", currentLang); }} catch (_) {{}}
        }}
        document.querySelectorAll("[data-lang-switch]").forEach((btn) => {{
          const active = btn.getAttribute("data-lang-switch") === currentLang;
          btn.classList.toggle("is-active", active);
          btn.setAttribute("aria-pressed", active ? "true" : "false");
        }});
        populateTaskSelect();
        renderScoreboardAndMatrix();
        renderReplay();
      }}

      btnScore.addEventListener("click", () => setView("scoreboard"));
      btnReplay.addEventListener("click", () => {{ setView("replay"); renderReplay(); }});
      selectEl.addEventListener("change", renderReplay);
      scrubber.addEventListener("input", renderReplay);

      document.querySelectorAll("[data-lang-switch]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          setLang(btn.getAttribute("data-lang-switch") || "en", true);
        }});
      }});

      document.querySelectorAll("[data-task-filter]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          currentTaskFilter = btn.getAttribute("data-task-filter") || "all";
          renderScoreboardAndMatrix();
        }});
      }});

      document.querySelectorAll("[data-replay-mode]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          currentReplayMode = btn.getAttribute("data-replay-mode") || "both";
          document.querySelectorAll("[data-replay-mode]").forEach((b) => {{
            b.classList.toggle("is-active", b === btn);
          }});
          renderReplay();
        }});
      }});

      btnTheme.addEventListener("click", () => {{
        const root = document.documentElement;
        root.setAttribute(
          "data-theme",
          root.getAttribute("data-theme") === "telemetry" ? "newsprint" : "telemetry"
        );
        syncThemeButtonLabel();
      }});

      const params = new URLSearchParams(window.location.search);
      const qLang = (params.get("lang") || "").toLowerCase();
      let savedLang = "";
      try {{ savedLang = (localStorage.getItem("skill-arena-lang") || "").toLowerCase(); }} catch (_) {{}}
      const rootDefault = (document.documentElement.getAttribute("data-default-lang") || "auto").toLowerCase();
      const navZh = ((navigator.language || "").toLowerCase().startsWith("zh"));

      let initialLang = "en";
      if (qLang.startsWith("zh")) initialLang = "zh";
      else if (qLang.startsWith("en")) initialLang = "en";
      else if (savedLang === "zh" || savedLang === "en") initialLang = savedLang;
      else if (rootDefault === "zh" || rootDefault === "en") initialLang = rootDefault;
      else if (navZh) initialLang = "zh";

      populateTaskSelect();
      if (params.get("task")) {{
        const found = tasks.findIndex(t => t.task_id === params.get("task"));
        if (found >= 0) selectEl.value = String(found);
      }}
      setLang(initialLang, false);
      if (params.get("view") === "replay") {{
        setView("replay");
      }}
    }})();
  </script>
</body>
</html>
"""
