"""Path-scrubbed deterministic JSON & bilingual Markdown verification reports."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import pwd
import re
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TMP_SYSTEM_PATH_RE = re.compile(r"/(?:private/)?(?:var/folders|tmp)/[^\s\"':;),\]}]+")
_USER_SYSTEM_PATH_RE = re.compile(r"/(?:Users|home)/[^\s/\"':;),\]}]+")

# Scrub volatile inline timing numbers inside tool summaries so JSON outside `timing_meta` stays byte-identical
_INLINE_TIMING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bin\s+\d+\.\d+\s*\(\d+\.\d+\)\s*seconds\b", re.IGNORECASE), "in <TIME>"),
    (re.compile(r"\bfinished\s+in\s+\d+(?:\.\d+)?(?:ms|s|\s+seconds)\b", re.IGNORECASE), "finished in <TIME>"),
    (re.compile(r"\bafter\s+\d+(?:\.\d+)?\s*seconds\b", re.IGNORECASE), "after <TIME>"),
    (re.compile(r"\bin\s+\d+(?:\.\d+)?(?:ms|s|\s+seconds)\b(?:\s*\(\d+:\d{2}:\d{2}\))?"), "in <TIME>"),
)

_PYTEST_STATUS_LABEL = (
    r"(?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?|reruns?|flaky|selected"
    r"|subtests?(?:\s+(?:passed|failed|skipped))?"
    r"|tests?\s+collected(?:\s*\(\d+\s+deselected\))?)"
)
_PYTEST_SUMMARY_CLAUSE = (
    rf"(?:\d+(?:/\d+)?\s+{_PYTEST_STATUS_LABEL}"
    r"|no\s+tests\s+(?:ran|collected)(?:\s*\(\d+\s+deselected\))?)"
)
# Normalize pytest-style `=== ... in <TIME> ===` summary banners whose `=` padding varies with duration string width
_PYTEST_SUMMARY_BANNER_RE = re.compile(
    rf"(?:^|(?<=\n)|(?<= \| ))[ \t]*=+[ \t]+"
    rf"(?P<summary>{_PYTEST_SUMMARY_CLAUSE}(?:,\s*{_PYTEST_SUMMARY_CLAUSE})*\s+in\s+<TIME>)"
    rf"[ \t]*=+[ \t]*(?=\r?\n| \| |$)"
)


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI CSI escape sequences from command output."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def normalize_inline_timings(text: str) -> str:
    """Replace volatile inline timing tokens with `<TIME>` and normalize pytest `=`-padded summary banners."""
    if not text:
        return ""
    cleaned = text
    for pattern, repl in _INLINE_TIMING_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    cleaned = _PYTEST_SUMMARY_BANNER_RE.sub(r"=== \g<summary> ===", cleaned)
    return cleaned


class PathScrubber:
    """Deterministic scrubber removing temporary paths, host user paths, ANSI codes, and inline durations."""

    def __init__(
        self,
        *,
        src_repo: Path | None = None,
        tmp_base: Path | None = None,
        tmp_repo: Path | None = None,
        tmp_home: Path | None = None,
        tmp_dir: Path | None = None,
    ) -> None:
        pw_home = pwd.getpwuid(os.getuid()).pw_dir
        env_home = os.path.expanduser("~")

        pairs: list[tuple[str, str]] = []

        def _add_path(p: Path | str | None, token: str) -> None:
            if not p:
                return
            raw = str(p)
            real = os.path.realpath(raw)
            for candidate in (real, raw):
                if candidate and candidate != "/":
                    pairs.append((candidate, token))

        _add_path(tmp_repo, "<TMP_REPO>")
        _add_path(tmp_home, "<TMP_HOME>")
        _add_path(tmp_dir, "<TMPDIR>")
        _add_path(tmp_base, "<TMP_ROOT>")
        _add_path(src_repo, "<REPO_ROOT>")
        _add_path(pw_home, "<USER_HOME>")
        _add_path(env_home, "<USER_HOME>")

        # Longest prefix first so `<TMP_ROOT>/repo` becomes `<TMP_REPO>`, not `<TMP_ROOT>/repo`
        dedup: dict[str, str] = {}
        for path_str, token in sorted(pairs, key=lambda kv: len(kv[0]), reverse=True):
            dedup.setdefault(path_str, token)
        self._replacements: list[tuple[str, str]] = list(dedup.items())

    def scrub_command(self, text: str) -> str:
        """Scrub known workspace/source/user paths in a command string without clobbering literal `/tmp/...` examples."""
        if not text:
            return ""
        cleaned = _ANSI_ESCAPE_RE.sub("", text)
        for src_str, token in self._replacements:
            cleaned = cleaned.replace(src_str, token)
        cleaned = _USER_SYSTEM_PATH_RE.sub("<USER_HOME>", cleaned)
        return cleaned

    def scrub(self, text: str, *, scrub_inline_timings: bool = True) -> str:
        if not text:
            return ""
        cleaned = _ANSI_ESCAPE_RE.sub("", text)
        for src_str, token in self._replacements:
            cleaned = cleaned.replace(src_str, token)
        cleaned = _TMP_SYSTEM_PATH_RE.sub("<TMPDIR>", cleaned)
        cleaned = _USER_SYSTEM_PATH_RE.sub("<USER_HOME>", cleaned)
        if scrub_inline_timings:
            cleaned = normalize_inline_timings(cleaned)
        return cleaned


@dataclass(slots=True)
class VerifyReport:
    """Complete verification report ready for deterministic JSON and bilingual Markdown serialization."""

    repo_name: str
    ref: str
    commit_sha: str
    conclusion: str  # "PASS" | "FAIL" | "ERROR"
    exit_code: int
    env_guard: dict[str, Any]
    build_and_test: dict[str, Any]
    readme_replay: dict[str, Any]
    mutation: dict[str, Any]
    self_report: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    timing_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "m2.v1",
            "repo_name": self.repo_name,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "conclusion": self.conclusion,
            "exit_code": self.exit_code,
            "env_guard": self.env_guard,
            "build_and_test": self.build_and_test,
            "readme_replay": self.readme_replay,
            "mutation": self.mutation,
            "self_report": self.self_report,
            "failures": self.failures,
            "timing_meta": self.timing_meta,
        }


def render_verify_json(report: VerifyReport) -> str:
    """Render structured JSON with `timing_meta` isolated at the bottom."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def _md_escape(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def _render_self_report_section(lines: list[str], sr: dict[str, Any], *, section_num: int, is_zh: bool) -> None:
    counts = sr.get("summary_counts", {})
    if is_zh:
        lines.append(f"## {section_num}. 自报用语核查（`{sr.get('report_file', '')}`）")
        lines.append(
            f"- 状态：**{sr['status']}** —— 检出绝对词条目 `{counts.get('total_matched_items', 0)}` 条"
            f"（无证据 `{counts.get('unsupported', 0)}` 条，有证据 `{counts.get('supported', 0)}` 条，"
            f"规则去噪跳过 `{counts.get('denoised', 0)}` 条）"
        )
        lines.append("")
        if sr.get("items"):
            lines.append("| 文件与行号 | 命中词 | 原句摘录 | 判定 | 证据核查说明 |")
            lines.append("|---|---|---|---|---|")
    else:
        lines.append(f"## {section_num}. Self-Report Wording Audit (`{sr.get('report_file', '')}`)")
        lines.append(
            f"- Status: **{sr['status']}** — `{counts.get('total_matched_items', 0)}` matched item(s) "
            f"(`{counts.get('unsupported', 0)}` unsupported, `{counts.get('supported', 0)}` supported, "
            f"`{counts.get('denoised', 0)}` denoised)"
        )
        lines.append("")
        if sr.get("items"):
            lines.append("| File & Line | Words | Original Sentence | Verdict | Evidence Detail |")
            lines.append("|---|---|---|---|---|")

    for item in sr.get("items", []):
        loc = f"`{item['file']}:L{item['line_no']}`"
        words_str = ", ".join(f"`{_md_escape(w)}`" for w in item["matched_words"])
        sent_str = _md_escape(item["sentence"])
        verdict_label = (
            ("无证据 (`UNSUPPORTED`)" if item["verdict"] == "UNSUPPORTED" else "有证据 (`SUPPORTED`)")
            if is_zh
            else f"`{item['verdict']}`"
        )
        lines.append(f"| {loc} | {words_str} | {sent_str} | **{verdict_label}** | {_md_escape(item['reason'])} |")


def render_verify_markdown(report: VerifyReport, *, lang: str = "zh") -> str:
    """Render deterministic path-scrubbed Markdown evidence report (no volatile wall-clock numbers in body)."""
    is_zh = lang == "zh"
    eg = report.env_guard
    bt = report.build_and_test
    rr = report.readme_replay
    mut = report.mutation
    sr = report.self_report

    lines: list[str] = []

    # Standalone `--self-report` mode (without `--repo`)
    if eg.get("status") == "SKIPPED":
        report_file = sr.get("report_file", report.repo_name)
        if is_zh:
            lines.append(f"# 自报用语核查报告：`{report_file}`")
            lines.append("")
            lines.append(f"- **总结论**：**{report.conclusion}**（退出码 `{report.exit_code}`）")
        else:
            lines.append(f"# Self-Report Wording Audit Report: `{report_file}`")
            lines.append("")
            lines.append(f"- **Conclusion**: **{report.conclusion}** (exit code `{report.exit_code}`)")
        lines.append("")
        _render_self_report_section(lines, sr, section_num=1, is_zh=is_zh)
        if report.failures:
            lines.append("")
            lines.append("## 2. 失败项汇总" if is_zh else "## 2. Failure Summary")
            for failure in report.failures:
                lines.append(f"- {failure}")
        lines.append("")
        return "\n".join(lines)

    uv_new_entries = report.timing_meta.get("uv_cache_new_entries", 0)
    sb_active = bool(eg.get("sandbox_exec_enabled", False))
    if is_zh:
        lines.append(f"# 仓库验收报告：`{report.repo_name}` @ `{report.commit_sha[:12]}`")
        lines.append("")
        lines.append(f"- **总结论**：**{report.conclusion}**（退出码 `{report.exit_code}`）")
        lines.append(f"- **校验引用**：`{report.ref}` -> `{report.commit_sha}`")
        lines.append("")
        lines.append("## 1. 干净拷贝与环境守卫")
        lines.append(f"- 克隆方式：`{eg['clone_method']}`")
        if sb_active:
            lines.append(
                "- 网络与家目录隔离：`sandbox-exec` 断网回放（仅放行本机 `localhost` 与 Unix 套接字；"
                "拦写真实家目录，仅放行 `<USER_HOME>/.cache/uv`）"
            )
        else:
            lines.append("- 网络与家目录隔离：离线环境变量隔离（非 macOS 宿主无 `sandbox-exec`）")
        lines.append(f"- 共用 `uv` 缓存（`<USER_HOME>/.cache/uv`）：本次新增 `{uv_new_entries}` 个条目")
        lines.append(
            f"- 源仓库只读指纹一致：`{eg['source_repo_untouched']}`"
            f"（`{eg['pre_fingerprint']}` == `{eg['post_fingerprint']}`）"
        )
        lines.append(f"- 临时目录已删除：`{eg['temp_copy_deleted']}`")
        lines.append(
            f"- 无残留后台进程：`{eg['no_leaked_processes']}`（检出残留进程数：`{eg['leaked_process_count']}`）"
        )
        lines.append("")
        lines.append("## 2. 构建与基线测试")
        lines.append("")
        lines.append("| 步骤 | 命令 | 退出码 | 结论 | 说明与输出摘要 |")
        lines.append("|---|---|---:|---|---|")
    else:
        lines.append(f"# Repository Verification Report: `{report.repo_name}` @ `{report.commit_sha[:12]}`")
        lines.append("")
        lines.append(f"- **Conclusion**: **{report.conclusion}** (exit code `{report.exit_code}`)")
        lines.append(f"- **Verified Ref**: `{report.ref}` -> `{report.commit_sha}`")
        lines.append("")
        lines.append("## 1. Clean Clone & Environment Guard")
        lines.append(f"- Clone method: `{eg['clone_method']}`")
        if sb_active:
            lines.append(
                "- Network & HOME guard: `sandbox-exec` offline replay (loopback & Unix sockets only; "
                "real HOME writes blocked except `<USER_HOME>/.cache/uv`)"
            )
        else:
            lines.append("- Network & HOME guard: offline env isolation (`sandbox-exec` unavailable)")
        lines.append(f"- Shared `uv` cache (`<USER_HOME>/.cache/uv`): `{uv_new_entries}` new entries added")
        lines.append(
            f"- Source repository untouched: `{eg['source_repo_untouched']}`"
            f" (`{eg['pre_fingerprint']}` == `{eg['post_fingerprint']}`)"
        )
        lines.append(f"- Temporary directory deleted: `{eg['temp_copy_deleted']}`")
        lines.append(
            f"- Zero leaked processes: `{eg['no_leaked_processes']}` (detected: `{eg['leaked_process_count']}`)"
        )
        lines.append("")
        lines.append("## 2. Build & Baseline Tests")
        lines.append("")
        lines.append("| Step | Command | Exit | Status | Reason & Output Summary |")
        lines.append("|---|---|---:|---|---|")

    for step_key in ("build", "test"):
        st = bt[step_key]
        cmd_str = f"`{_md_escape(st['command'])}`" if st["command"] else "—"
        ec_str = str(st["exit_code"]) if st["exit_code"] is not None else "—"
        detail = st["reason"]
        if st["output_summary"]:
            detail = f"{detail} — `{_md_escape(st['output_summary'])}`"
        lines.append(f"| `{step_key}` | {cmd_str} | {ec_str} | **{st['status']}** | {detail} |")

    lines.append("")
    if is_zh:
        lines.append("## 3. README 命令逐条回放证据表")
        lines.append("- 回放模式：`sandbox-exec` 断网回放（禁止外网连接，仅放行本机回环）")
        lines.append(
            f"- 根级 README 文件：{', '.join(f'`{n}`' for n in rr['discovered_readmes']) or '无'}"
        )
        lines.append(
            f"- 双语命令序列一致性：**{rr['bilingual_parity']['status']}**（{rr['bilingual_parity']['reason']}）"
        )
        counts = rr["summary_counts"]
        lines.append(
            f"- 代码块统计：共 `{rr['fenced_block_count']}` 块（Shell 命令块 `{rr['shell_block_count']}`，"
            f"非命令块 `{rr['non_command_block_count']}`）"
        )
        lines.append(
            f"- 命令分类汇总：共 `{counts['total_commands']}` 条 —— "
            f"回放通过 `{counts['executed_pass']}`，回放失败 `{counts['executed_fail']}`，"
            f"无法验证 `{counts['unverifiable']}`，显式豁免 `{counts['skipped_user']}`，"
            f"双语重复跳过 `{counts['skipped_parity_duplicate']}`"
        )
        if rr["skip_cmd_patterns"]:
            lines.append(f"- `--skip-cmd` 豁免规则：{', '.join(f'`{p}`' for p in rr['skip_cmd_patterns'])}")
            for ex in rr["exempted_commands"]:
                lines.append(f"  - 已豁免：`{_md_escape(ex)}`")
        lines.append("")
        lines.append("| # | 位置 | 命令 | 类别 | 退出码 | 结论 | 证据或无法验证原因 |")
        lines.append("|---:|---|---|---|---:|---|---|")
    else:
        lines.append("## 3. README Command Replay Evidence")
        lines.append("- Replay mode: `sandbox-exec` offline replay (external network blocked, loopback allowed)")
        lines.append(
            f"- Root README files: {', '.join(f'`{n}`' for n in rr['discovered_readmes']) or 'none'}"
        )
        lines.append(
            f"- Bilingual command parity: **{rr['bilingual_parity']['status']}** ({rr['bilingual_parity']['reason']})"
        )
        counts = rr["summary_counts"]
        lines.append(
            f"- Code blocks: `{rr['fenced_block_count']}` total (`{rr['shell_block_count']}` shell, "
            f"`{rr['non_command_block_count']}` non-command)"
        )
        lines.append(
            f"- Command totals: `{counts['total_commands']}` total — "
            f"`{counts['executed_pass']}` passed, `{counts['executed_fail']}` failed, "
            f"`{counts['unverifiable']}` unverifiable, `{counts['skipped_user']}` user-skipped, "
            f"`{counts['skipped_parity_duplicate']}` parity-deduped"
        )
        if rr["skip_cmd_patterns"]:
            lines.append(f"- `--skip-cmd` patterns: {', '.join(f'`{p}`' for p in rr['skip_cmd_patterns'])}")
            for ex in rr["exempted_commands"]:
                lines.append(f"  - Exempted: `{_md_escape(ex)}`")
        lines.append("")
        lines.append("| # | Location | Command | Category | Exit | Status | Evidence or Unverifiable Reason |")
        lines.append("|---:|---|---|---|---:|---|---|")

    for item in rr["items"]:
        loc = f"`{item['doc_name']}:L{item['line_no']}`"
        cmd_cell = f"`{_md_escape(item['command'])}`"
        ec_cell = str(item["exit_code"]) if item["exit_code"] is not None else "—"
        ev_text = _md_escape(item["reason"])
        if item["output_summary"]:
            ev_text = f"{ev_text} (`{_md_escape(item['output_summary'])}`)"
        lines.append(
            f"| {item['index']} | {loc} | {cmd_cell} | `{item['category']}` | {ec_cell} | **{item['status']}** | {ev_text} |"
        )

    lines.append("")
    if is_zh:
        lines.append("## 4. 破坏性变异反证")
        lines.append(f"- 状态：**{mut['status']}**（{mut['reason']}）")
        if mut.get("items"):
            lines.append("")
            lines.append("| # | 文件与行号 | 变异算子与改动 | 测试命令 | 改坏后结果 | 还原与工作树校验 |")
            lines.append("|---:|---|---|---|---|---|")
    else:
        lines.append("## 4. Destructive Mutation Verification")
        lines.append(f"- Status: **{mut['status']}** ({mut['reason']})")
        if mut.get("items"):
            lines.append("")
            lines.append("| # | File & Line | Operator & Mutation | Test Command | Mutated Verdict | Restore & Clean Check |")
            lines.append("|---:|---|---|---|---|---|")

    for m_item in mut.get("items", []):
        m_loc = f"`{m_item['file']}:L{m_item['line_no']}`"
        m_desc = f"`{m_item['operator']}`: {_md_escape(m_item['description'])}"
        m_cmd = f"`{_md_escape(m_item['test_command'])}`"
        m_ec = str(m_item["mutated_exit_code"]) if m_item["mutated_exit_code"] is not None else "—"
        m_verdict = f"**{m_item['verdict']}** (exit={m_ec})"
        if m_item.get("mutated_output_summary"):
            m_verdict += f" — `{_md_escape(m_item['mutated_output_summary'])}`"
        m_restore = f"restored_pass={m_item['restored_tests_passed']}, clean={m_item['worktree_clean']}"
        lines.append(f"| {m_item['index']} | {m_loc} | {m_desc} | {m_cmd} | {m_verdict} | `{m_restore}` |")

    next_sec = 5
    if sr.get("enabled"):
        lines.append("")
        _render_self_report_section(lines, sr, section_num=next_sec, is_zh=is_zh)
        next_sec += 1

    if report.failures:
        lines.append("")
        lines.append(f"## {next_sec}. 失败项汇总" if is_zh else f"## {next_sec}. Failure Summary")
        for failure in report.failures:
            lines.append(f"- {failure}")

    lines.append("")
    return "\n".join(lines)
