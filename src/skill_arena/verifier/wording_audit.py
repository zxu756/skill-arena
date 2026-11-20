"""Deterministic self-report wording auditor enforcing same-item command+output evidence and 4 denoising rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any

from skill_arena.verifier.report import PathScrubber

# Canonical 10-keyword vocabulary (M2 item 6 / P5):
# Rule 1 denoising built into `零` (`(?<!非)零(?![散碎点])`) and `zero` (`(?<!non-)(?<!non)\bzero\b`).
KEYWORD_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("彻底", re.compile(r"彻底")),
    ("全部", re.compile(r"全部")),
    ("零", re.compile(r"(?<!非)零(?![散碎点])")),
    ("已修", re.compile(r"已修")),
    ("绝不", re.compile(r"绝不")),
    ("无任何", re.compile(r"无任何")),
    ("100%", re.compile(r"(?<!\d)100%")),
    ("never", re.compile(r"\bnever\b", re.IGNORECASE)),
    ("zero", re.compile(r"(?<!non-)(?<!non)\bzero\b", re.IGNORECASE)),
    ("completely", re.compile(r"\bcompletely\b", re.IGNORECASE)),
)

# Rule 3: Out-of-scope / unverified section headings & disclaimer bullets
_OUT_OF_SCOPE_HEADING_RE = re.compile(
    r"(?:没验证|未验证|不在.*范围|未纳入|计划中|当前未实现|诚实.*限制|排期在|"
    r"out\s+of\s+scope|not\s+verified|not\s+yet\s+implemented|planned|limitations)",
    re.IGNORECASE,
)
_OUT_OF_SCOPE_BULLET_HEADER_RE = re.compile(
    r"^\s*[-*+]\s+\*\*(?:没验证的项|未验证的项|冻结)[^*]*\*\*",
    re.IGNORECASE,
)
_OUT_OF_SCOPE_LEADING_DISCLAIMER_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*)?(?:"
    r"按\s+[^\n，,：:]{1,60}(?:排在|不在\s*M?\d*\s*范围)|"
    r"不在\s*M?\d*\s*范围|"
    r"未验证|尚未(?:在真机上)?验证|冻结（不在|"
    r"out\s+of\s+scope|not\s+verified|planned\s+for\s+M\d+"
    r")",
    re.IGNORECASE,
)
_AFFIRMATIVE_COMPLETION_BOAST_RE = re.compile(
    r"(?:全部.*(?:实际执行并)?通过|均已.*实际执行并通过|无任何跳过或未跑项|无[“\"']?没跑[”\"']?项)",
    re.IGNORECASE,
)

# Rule 4: Quoted deleted / retracted legacy strings
_DELETION_CONTEXT_RE = re.compile(
    r"(?:删除|移除|撤回|清除|去掉|删掉|移出|下线|更正.*原报告|曾写|原写|写有|硬编码了|"
    r"\b(?:remov(?:e|ed|ing)|delet(?:e|ed|ing)|retract(?:ed|ing)?|strip(?:ped|ping)?|drop(?:ped|ping)?|formerly\s+wrote)\b)",
    re.IGNORECASE,
)
_QUOTED_SPAN_RE = re.compile(
    r'(?:"[^"\n]{1,240}"|“[^”\n]{1,240}”|‘[^’\n]{1,240}’|「[^」\n]{1,240}」|『[^』\n]{1,240}』|'
    r"`[^`\n]{1,240}`|\*\"[^\"\n]{1,240}\"\*|'[^'\n]{1,160}')"
)
_INLINE_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")

# Same-item command + output evidence patterns
# Command: inline backticked runnable invocation (command + args/path, never bare flag/word) OR fenced shell block
_INLINE_RUNNABLE_CMD_RE = re.compile(
    r"`[^`\n]*(?:"
    r"\b(?:uv\s+(?:run|sync|tool)|swift\s+(?:build|test|run)|cargo\s+(?:build|test|run)|"
    r"pytest\s+\S+|python3?\s+(?:-\S+|\S+\.py\b)|bash\s+\S+|sh\s+\S+|"
    r"git\s+(?:diff|status|grep|show|clone|rev-parse)\s+\S+|"
    r"rg\s+\S+|grep\s+-\S+|sqlite3\s+\S+|shasum\s+\S+|otool\s+-\S+|codesign\s+-\S+|nm\s+-\S+|curl\s+\S+|"
    r"skill-arena\s+\S+)|"
    r"(?:\./)?Scripts/\S+|\.build/\S+|"
    r"(?:(?<=`)|(?<=\s))(?:[A-Za-z0-9_./-]+\s+)?--cli\s+[A-Za-z0-9_-]+"
    r")[^`\n]*`",
    re.IGNORECASE,
)
_FENCED_SHELL_CMD_RE = re.compile(r"```(?:bash|sh|zsh|shell|console)\b", re.IGNORECASE)

# Output: explicit exit/return code, >0 passed test count, 0-match grep count, SHA-256 digest, or table exit-code cell
_OUTPUT_EVIDENCE_RE = re.compile(
    r"(?:退出码[：:\s]*`?\d+`?|返回码[：:\s]*`?\d+`?|"
    r"\bexit(?:\s+code|_code)\s*[=:]?\s*`?\d+`?|\bexit\s*=\s*`?\d+`?|\bexitCode\s*==\s*`?\d+`?|\brc\s*=\s*`?\d+`?|"
    r"\|\s*(?:[^|\n]{0,14}\s)?`?[012]`?\s*\||"
    r"\b[1-9]\d*\s+passed\b|\b[1-9]\d*/[1-9]\d*\s*(?:条测试)?(?:通过|passed)\b|\bRan\s+[1-9]\d*\s+tests?\b|"
    r"\b0\s+matches\b|\b0\s*处(?:命中|匹配)|"
    r"sha256\s*=\s*[0-9a-f]{8,}|SHA-256\s*(?:均为|为)?\s*`[0-9a-f]{8,}`|"
    r"\bbefore\s*=\s*\d+\s*,\s*after\s*=\s*\d+)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class LogicalMarkdownItem:
    """One logical unit in a Markdown self-report (heading, table row, list item, paragraph, or code block)."""

    start_line: int
    end_line: int
    kind: str  # "heading" | "table_row" | "bullet" | "paragraph" | "code_block"
    indent: int
    lines: list[str]
    section_heading: str
    under_out_of_scope_heading: bool
    under_out_of_scope_bullet: bool


@dataclass(slots=True)
class WordingFinding:
    """Audit verdict for a single logical item matching one or more absolute-claim keywords."""

    file: str
    line_no: int
    kind: str
    matched_words: list[str]
    sentence: str
    has_command: bool
    has_output: bool
    verdict: str  # "UNSUPPORTED" | "SUPPORTED"
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line_no": self.line_no,
            "kind": self.kind,
            "matched_words": list(self.matched_words),
            "sentence": self.sentence,
            "has_command": self.has_command,
            "has_output": self.has_output,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(slots=True)
class WordingAuditSummary:
    """Complete result of auditing a Markdown self-report file."""

    enabled: bool
    status: str  # "PASS" | "FAIL"
    report_file: str
    total_matched_items: int
    unsupported_count: int
    supported_count: int
    denoised_count: int
    denoised_by_rule: dict[str, int] = field(default_factory=dict)
    items: list[WordingFinding] = field(default_factory=list)
    wall_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "report_file": self.report_file,
            "summary_counts": {
                "total_matched_items": self.total_matched_items,
                "unsupported": self.unsupported_count,
                "supported": self.supported_count,
                "denoised": self.denoised_count,
                "denoised_by_rule": dict(self.denoised_by_rule),
            },
            "items": [item.to_dict() for item in self.items],
        }


def split_markdown_logical_items(md_text: str) -> list[LogicalMarkdownItem]:
    """Split Markdown into deterministic logical items while parsing ` ```markdown ` draft blocks as prose."""
    lines = md_text.splitlines()
    items: list[LogicalMarkdownItem] = []
    in_fence = False
    fence_marker = ""
    fence_lang = ""
    cur_heading = ""
    under_oos_heading = False
    oos_bullet_indent: int | None = None
    cur: LogicalMarkdownItem | None = None

    def _flush() -> None:
        nonlocal cur
        if cur is not None:
            items.append(cur)
            cur = None

    for line_no, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not in_fence:
            m_fence = re.match(r"^(\s*)(`{3,}|~{3,})([^\n`~]*)$", raw_line)
            if m_fence:
                info = m_fence.group(3).strip()
                lang_tag = info.split()[0].lower() if info else ""
                in_fence = True
                fence_marker = m_fence.group(2)
                fence_lang = lang_tag
                if lang_tag in {"markdown", "md"}:
                    _flush()
                elif cur is not None and cur.kind in {"bullet", "paragraph"}:
                    cur.lines.append(raw_line)
                    cur.end_line = line_no
                else:
                    cur = LogicalMarkdownItem(
                        start_line=line_no,
                        end_line=line_no,
                        kind="code_block",
                        indent=len(m_fence.group(1)),
                        lines=[raw_line],
                        section_heading=cur_heading,
                        under_out_of_scope_heading=under_oos_heading,
                        under_out_of_scope_bullet=oos_bullet_indent is not None,
                    )
                continue
        else:
            if stripped.startswith(fence_marker[0] * len(fence_marker)) and set(stripped) == {fence_marker[0]}:
                if fence_lang not in {"markdown", "md"} and cur is not None:
                    cur.lines.append(raw_line)
                    cur.end_line = line_no
                    if cur.kind == "code_block":
                        _flush()
                else:
                    _flush()
                in_fence = False
                fence_marker = ""
                fence_lang = ""
                continue
            if fence_lang not in {"markdown", "md"}:
                if cur is not None:
                    cur.lines.append(raw_line)
                    cur.end_line = line_no
                continue

        # Strip optional blockquote prefix (`> `) so quoted draft bullets/headings parse at exact line numbers
        work_line = re.sub(r"^\s*>\s?", "", raw_line)
        w_stripped = work_line.strip()

        m_head = re.match(r"^\s*(#{1,6})\s+(.+)$", work_line)
        if m_head:
            _flush()
            cur_heading = w_stripped
            under_oos_heading = bool(_OUT_OF_SCOPE_HEADING_RE.search(cur_heading))
            oos_bullet_indent = None
            items.append(
                LogicalMarkdownItem(
                    start_line=line_no,
                    end_line=line_no,
                    kind="heading",
                    indent=0,
                    lines=[raw_line],
                    section_heading=cur_heading,
                    under_out_of_scope_heading=under_oos_heading,
                    under_out_of_scope_bullet=False,
                )
            )
            continue

        if w_stripped.startswith("|") and w_stripped.endswith("|"):
            _flush()
            if re.match(r"^\|[\s:\-\|]+\|$", w_stripped):
                continue
            items.append(
                LogicalMarkdownItem(
                    start_line=line_no,
                    end_line=line_no,
                    kind="table_row",
                    indent=0,
                    lines=[raw_line],
                    section_heading=cur_heading,
                    under_out_of_scope_heading=under_oos_heading,
                    under_out_of_scope_bullet=False,
                )
            )
            continue

        m_bullet = re.match(r"^(\s*)(?:[-*+]|\d+[.)])\s+", work_line)
        if m_bullet:
            _flush()
            b_indent = len(m_bullet.group(1))
            if oos_bullet_indent is not None and b_indent <= oos_bullet_indent:
                oos_bullet_indent = None
            if _OUT_OF_SCOPE_BULLET_HEADER_RE.match(work_line):
                oos_bullet_indent = b_indent
            cur = LogicalMarkdownItem(
                start_line=line_no,
                end_line=line_no,
                kind="bullet",
                indent=b_indent,
                lines=[raw_line],
                section_heading=cur_heading,
                under_out_of_scope_heading=under_oos_heading,
                under_out_of_scope_bullet=oos_bullet_indent is not None,
            )
            continue

        if not w_stripped:
            if cur is not None and cur.kind == "paragraph":
                _flush()
            elif cur is not None and cur.kind == "bullet":
                cur.lines.append(raw_line)
                cur.end_line = line_no
            continue

        lead_spaces = len(work_line) - len(work_line.lstrip(" \t"))
        if cur is not None and cur.kind == "bullet" and lead_spaces > cur.indent:
            cur.lines.append(raw_line)
            cur.end_line = line_no
            continue

        if cur is not None and cur.kind == "paragraph":
            cur.lines.append(raw_line)
            cur.end_line = line_no
        else:
            _flush()
            oos_bullet_indent = None
            cur = LogicalMarkdownItem(
                start_line=line_no,
                end_line=line_no,
                kind="paragraph",
                indent=lead_spaces,
                lines=[raw_line],
                section_heading=cur_heading,
                under_out_of_scope_heading=under_oos_heading,
                under_out_of_scope_bullet=False,
            )

    _flush()
    return items


def _extract_prose_outside_code_fences(lines: list[str]) -> str:
    prose_lines: list[str] = []
    in_cb = False
    for ln in lines:
        if ln.strip().startswith(("```", "~~~")):
            in_cb = not in_cb
            continue
        if not in_cb:
            prose_lines.append(ln)
    return "\n".join(prose_lines).strip()


def _compact_sentence(prose: str, scrubber: PathScrubber, max_chars: int = 240) -> str:
    collapsed = re.sub(r"\s+", " ", prose).strip()
    scrubbed = scrubber.scrub(collapsed, scrub_inline_timings=False)
    if len(scrubbed) > max_chars:
        return scrubbed[: max_chars - 1] + "…"
    return scrubbed


def audit_self_report_file(
    md_path: Path,
    *,
    scrubber: PathScrubber | None = None,
    lang: str = "zh",
) -> WordingAuditSummary:
    """Audit `md_path` for unsupported absolute claims using the 10-word list and 4 denoising rules."""
    is_zh = lang == "zh"
    t0 = time.monotonic()
    eff_scrubber = scrubber or PathScrubber()
    text = md_path.read_text(encoding="utf-8", errors="replace")
    items = split_markdown_logical_items(text)

    findings: list[WordingFinding] = []
    denoised_by_rule: dict[str, int] = {
        "non_zero": 0,
        "heading_or_leadin": 0,
        "out_of_scope": 0,
        "quoted_deleted": 0,
    }

    for idx, item in enumerate(items):
        if item.kind == "code_block":
            continue
        raw_block = "\n".join(item.lines)
        prose = _extract_prose_outside_code_fences(item.lines)
        if not prose:
            continue

        prose_no_code = _INLINE_CODE_SPAN_RE.sub(" ", prose)

        # Count Rule 1 ("非零" / "零散" / "零点" / "non-zero") denoising when literal 零/zero appeared only in excluded compounds
        if ("零" in prose_no_code or re.search(r"\bzero\b", prose_no_code, re.IGNORECASE)) and not any(
            rx.search(prose_no_code) for _, rx in KEYWORD_SPECS
        ):
            denoised_by_rule["non_zero"] += 1
            continue

        # Rule 2: Headings & table/code/list lead-in sentences ending with `:` / `：`
        if item.kind == "heading":
            if any(rx.search(prose_no_code) for _, rx in KEYWORD_SPECS):
                denoised_by_rule["heading_or_leadin"] += 1
            continue

        next_kind = items[idx + 1].kind if idx + 1 < len(items) else None
        if prose.rstrip().endswith(("：", ":")) and next_kind in {"table_row", "code_block", "bullet"}:
            if any(rx.search(prose_no_code) for _, rx in KEYWORD_SPECS):
                denoised_by_rule["heading_or_leadin"] += 1
            continue

        # Rule 3: Out-of-scope / unverified sections or disclaimers (unless boasting 100% completion!)
        in_oos_context = (
            item.under_out_of_scope_heading
            or item.under_out_of_scope_bullet
            or bool(_OUT_OF_SCOPE_LEADING_DISCLAIMER_RE.search(prose))
        )
        if in_oos_context and not _AFFIRMATIVE_COMPLETION_BOAST_RE.search(prose):
            if any(rx.search(prose_no_code) for _, rx in KEYWORD_SPECS):
                denoised_by_rule["out_of_scope"] += 1
            continue

        # Rule 4: Strip quoted/backticked spans describing deleted or retracted old copy
        had_kw_before_quote_strip = any(rx.search(prose_no_code) for _, rx in KEYWORD_SPECS)
        scan_text = prose
        if _DELETION_CONTEXT_RE.search(scan_text):
            scan_text = _QUOTED_SPAN_RE.sub(" ", scan_text)
        scan_text = _INLINE_CODE_SPAN_RE.sub(" ", scan_text)

        matched_words = [label for label, rx in KEYWORD_SPECS if rx.search(scan_text)]
        if not matched_words:
            if had_kw_before_quote_strip:
                denoised_by_rule["quoted_deleted"] += 1
            continue

        has_cmd = bool(_INLINE_RUNNABLE_CMD_RE.search(raw_block) or _FENCED_SHELL_CMD_RE.search(raw_block))
        has_out = bool(_OUTPUT_EVIDENCE_RE.search(raw_block))
        if has_cmd and has_out:
            verdict = "SUPPORTED"
            reason = (
                "有证据：同一逻辑条目内含可执行命令及退出码/输出摘要"
                if is_zh
                else "Supported: item contains both runnable command and exit-code/output evidence"
            )
        else:
            verdict = "UNSUPPORTED"
            missing_parts: list[str] = []
            if not has_cmd:
                missing_parts.append("缺少命令" if is_zh else "missing command")
            if not has_out:
                missing_parts.append("缺少输出/退出码" if is_zh else "missing output/exit code")
            joined_missing = "、".join(missing_parts) if is_zh else ", ".join(missing_parts)
            reason = f"无证据（{joined_missing}）" if is_zh else f"Unsupported ({joined_missing})"

        findings.append(
            WordingFinding(
                file=md_path.name,
                line_no=item.start_line,
                kind=item.kind,
                matched_words=matched_words,
                sentence=_compact_sentence(prose, eff_scrubber),
                has_command=has_cmd,
                has_output=has_out,
                verdict=verdict,
                reason=reason,
            )
        )

    unsupported_n = sum(1 for f in findings if f.verdict == "UNSUPPORTED")
    supported_n = sum(1 for f in findings if f.verdict == "SUPPORTED")
    denoised_total = sum(denoised_by_rule.values())
    wall_ms = int(round((time.monotonic() - t0) * 1000))

    return WordingAuditSummary(
        enabled=True,
        status="FAIL" if unsupported_n > 0 else "PASS",
        report_file=md_path.name,
        total_matched_items=len(findings),
        unsupported_count=unsupported_n,
        supported_count=supported_n,
        denoised_count=denoised_total,
        denoised_by_rule=denoised_by_rule,
        items=findings,
        wall_ms=wall_ms,
    )
