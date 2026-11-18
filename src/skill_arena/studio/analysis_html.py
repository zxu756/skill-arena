"""Server-rendered experiment-design panel: per-arm failure categories and the other design metrics.

The panel is rendered in both languages; the page's language switch shows one block and hides the
other (``data-lang-block``), so no translation logic has to live in JavaScript.
"""

from __future__ import annotations

import html
from typing import Any

FAILURE_KIND_LABELS: dict[str, tuple[str, str]] = {
    "passed": ("Passed", "通过"),
    "wrong": ("Wrong (tests fail)", "答错（测试不过）"),
    "interrupted": ("Interrupted (time limit)", "超时打断"),
    "anti_cheat": ("Anti-cheat", "违规（防作弊）"),
    "craft": ("Craft gate only", "仅工艺检查不过"),
    "infra": ("Infra (CLI, quota, no output)", "环境故障（CLI、额度、无输出）"),
    "grader_error": ("Grader error", "判分出错"),
}


def _esc(val: Any) -> str:
    return html.escape(str(val))


def _pp(val: float | None) -> str:
    return "n/a" if val is None else f"{float(val) * 100.0:+.1f} pp"


def _pct(val: float | None) -> str:
    return "n/a" if val is None else f"{float(val) * 100.0:.1f}%"


def _arm_name(row: dict[str, Any], is_zh: bool) -> str:
    role = str(row.get("role", ""))
    role_txt = ("实验组" if role == "treatment" else "对照组") if is_zh else ("Treatment" if role == "treatment" else "Control")
    return f"{role_txt} · {row.get('label', '')}"


def _table(headers: list[str], rows: list[list[str]], *, first_col_plain: bool = True) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_rows = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            cls = "" if (i == 0 and first_col_plain) else ' class="mono"'
            cells.append(f"<td{cls}>{_esc(c)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<div class="analysis-scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _failure_block(report: dict[str, Any], is_zh: bool) -> str:
    rows = list(report.get("leaderboard") or [])
    kinds = list(FAILURE_KIND_LABELS)
    headers = ["参赛组别" if is_zh else "Arm"] + [FAILURE_KIND_LABELS[k][1 if is_zh else 0] for k in kinds]
    body = []
    for row in rows:
        counts = dict(row.get("failure_counts") or {})
        body.append([_arm_name(row, is_zh)] + [str(int(counts.get(k, 0))) for k in kinds])
    title = "失败原因（每组试验数）" if is_zh else "Failure categories (trials per arm)"
    return f'<h3 class="analysis-h">{_esc(title)}</h3>' + _table(headers, body)


def _comparisons_block(report: dict[str, Any], is_zh: bool) -> str:
    comps = list(report.get("comparisons") or [])
    if not comps:
        return ""
    headers = (
        ["实验组", "配对题数", "Pass@1 差值", "95% 区间", "置换检验 p", "Holm 校正 p", "结论"]
        if is_zh
        else ["Treatment", "Paired tasks", "Pass@1 delta", "95% CI", "Permutation p", "Holm p", "Verdict"]
    )
    body = []
    for c in comps:
        body.append(
            [
                str(c.get("treatment_label") or c.get("treatment_id") or ""),
                str(int(c.get("tasks_paired", 0))),
                _pp(c.get("delta_pass_rate")),
                f"[{_pp(c.get('ci_low'))}, {_pp(c.get('ci_high'))}]",
                f"{float(c.get('p_value', 1.0)):.4f}",
                f"{float(c.get('p_value_holm', c.get('p_value', 1.0))):.4f}",
                str(c.get("verdict_label_zh" if is_zh else "verdict_label_en", "")),
            ]
        )
    title = "各实验组对比对照组（按题配对）" if is_zh else "Each treatment vs control (task-paired)"
    foot = (
        "p 值来自按题差值的符号翻转置换检验（不超过 16 道非零差值题时精确枚举，否则 20000 次固定种子抽样）；"
        "Holm 校正控制多个实验组的总体误判率。"
        if is_zh
        else "p from a sign-flip permutation test on per-task differences (exact up to 16 non-zero tasks, "
        "else 20000 seeded draws); Holm adjusts for testing several treatments."
    )
    return f'<h3 class="analysis-h">{_esc(title)}</h3>' + _table(headers, body) + f'<p class="analysis-foot">{_esc(foot)}</p>'


def _time_curve_block(report: dict[str, Any], is_zh: bool) -> str:
    rows = list(report.get("leaderboard") or [])
    curves = [list(r.get("pass_at_time") or []) for r in rows]
    if not any(curves):
        return ""
    longest = max(curves, key=len)
    headers = ["参赛组别" if is_zh else "Arm"]
    for pt in longest:
        t_sec = pt.get("t_sec")
        txt = f"{t_sec}s" if t_sec is not None else ("时限" if is_zh else "limit")
        if pt.get("is_full_limit") and t_sec is not None:
            txt += "（时限）" if is_zh else " (limit)"
        headers.append(f"Pass@{txt}")
    body = []
    for row, curve in zip(rows, curves):
        by_t = {pt.get("t_sec"): pt for pt in curve}
        body.append([_arm_name(row, is_zh)] + [_pct(by_t[pt.get("t_sec")]["pass_rate"]) if pt.get("t_sec") in by_t else "n/a" for pt in longest])
    title = "时限曲线 Pass@T" if is_zh else "Time-limit curve Pass@T"
    foot = (
        "只有通过且智能体在 T 秒内结束的试验才算在 T 时通过；曲线在时限前已经变平，说明时限够用。"
        if is_zh
        else "A trial counts at T only if it passed and the agent finished within T seconds; "
        "a curve that is flat well before the limit means the limit was long enough."
    )
    return f'<h3 class="analysis-h">{_esc(title)}</h3>' + _table(headers, body) + f'<p class="analysis-foot">{_esc(foot)}</p>'


def _agent_tests_block(report: dict[str, Any], is_zh: bool) -> str:
    rows = list(report.get("leaderboard") or [])
    headers = (
        ["参赛组别", "写了测试的试验", "在参考实现上有效", "平均变异得分", "杀死变异体"]
        if is_zh
        else ["Arm", "Trials with own tests", "Valid on reference", "Mean mutation score", "Mutants killed"]
    )
    body = []
    for row in rows:
        q = dict(row.get("agent_test_quality") or {})
        body.append(
            [
                _arm_name(row, is_zh),
                f"{int(q.get('trials_with_tests', 0))}/{int(q.get('trials', 0))}",
                _pct(q.get("valid_rate")),
                _pct(q.get("mean_mutation_score")),
                f"{int(q.get('mutants_killed', 0))}/{int(q.get('mutants_total', 0))}",
            ]
        )
    title = "智能体自写测试质量（变异得分）" if is_zh else "Agent-written tests (mutation score)"
    foot = (
        "智能体新增的测试先在参考实现上运行（失败即无效），再在每个变异体上运行；测试失败即杀死该变异体。"
        if is_zh
        else "The agent's new tests run against the reference solution (failing there makes them invalid) "
        "and against every mutant; a mutant is killed when the tests fail on it."
    )
    return f'<h3 class="analysis-h">{_esc(title)}</h3>' + _table(headers, body) + f'<p class="analysis-foot">{_esc(foot)}</p>'


def _blocks(report: dict[str, Any], is_zh: bool) -> list[str]:
    return [
        _comparisons_block(report, is_zh),
        _failure_block(report, is_zh),
        _time_curve_block(report, is_zh),
        _agent_tests_block(report, is_zh),
    ]


def render_design_analysis(report: dict[str, Any], render_lang: str) -> str:
    """Return the experiment-design panel with an English and a Chinese block."""
    parts = []
    for lang in ("en", "zh"):
        is_zh = lang == "zh"
        hidden = "" if lang == render_lang else " hidden"
        heading = "实验设计分析" if is_zh else "Experiment design analysis"
        note = (
            "失败原因、时限曲线、自写测试质量、行为指标与分类别差值"
            if is_zh
            else "Failure categories, time-limit curve, agent test quality, behaviour and per-category deltas"
        )
        inner = "".join(_blocks(report, is_zh))
        parts.append(
            f'<div data-lang-block="{lang}"{hidden}>'
            f'<div class="panel-head"><h2>{_esc(heading)}</h2>'
            f'<span class="panel-note mono">{_esc(note)}</span></div>'
            f"{inner}</div>"
        )
    return f'<section class="panel analysis-panel" id="design-analysis" style="margin-top: 20px;">{"".join(parts)}</section>'
