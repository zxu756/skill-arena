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


def _blocks(report: dict[str, Any], is_zh: bool) -> list[str]:
    return [_failure_block(report, is_zh)]


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
