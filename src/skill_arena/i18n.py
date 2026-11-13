"""Bilingual (zh-CN / en) locale detection and CLI/UI translations for SkillArena."""

from __future__ import annotations

from collections.abc import Mapping
import os
import unicodedata

SUPPORTED_LANG_CHOICES = ("auto", "zh", "en")


def _parse_lang_tag(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lower().replace("_", "-")
    if not cleaned or cleaned in {"auto", "c", "posix"}:
        return None
    if cleaned.startswith("zh"):
        return "zh"
    if cleaned.startswith("en"):
        return "en"
    return None


def resolve_lang(cli_lang: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve active language ('zh' or 'en') from CLI flag, SKILL_ARENA_LANG, or POSIX locale."""
    environ = os.environ if env is None else env

    if cli_lang and cli_lang != "auto":
        parsed = _parse_lang_tag(cli_lang)
        if parsed:
            return parsed

    env_override = _parse_lang_tag(environ.get("SKILL_ARENA_LANG"))
    if env_override:
        return env_override

    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = environ.get(var, "").strip()
        if val:
            parsed = _parse_lang_tag(val)
            if parsed:
                return parsed
            if val.lower() not in {"c", "posix", "c.utf-8"}:
                return "en"

    return "en"


def resolve_html_lang_mode(
    cli_lang: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return (initial_render_lang, data_default_lang_attr) for exported/served HTML."""
    environ = os.environ if env is None else env

    if cli_lang and cli_lang != "auto":
        parsed = _parse_lang_tag(cli_lang) or "en"
        return parsed, parsed

    env_override = _parse_lang_tag(environ.get("SKILL_ARENA_LANG"))
    if env_override:
        return env_override, env_override

    detected = resolve_lang("auto", environ)
    if detected == "zh":
        return "zh", "zh"
    return "en", "auto"


def display_width(text: str) -> int:
    """Compute terminal column width accounting for East Asian Wide/Fullwidth characters."""
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in {"W", "F"}:
            width += 2
        else:
            width += 1
    return width


def pad_cell(text: str, width: int, align: str = "left") -> str:
    """Pad string to exact terminal display width."""
    pad = max(0, width - display_width(text))
    if align == "right":
        return (" " * pad) + text
    return text + (" " * pad)


CATEGORY_ZH: dict[str, str] = {
    "concurrency": "并发控制",
    "frontend_craft": "前端工艺",
    "refactor_test": "重构测试",
    "security_hygiene": "安全加固",
    "git_extracted": "Git 历史提取",
    "general": "通用工程",
}

TASK_TITLE_ZH: dict[str, str] = {
    "conc-01-token-bucket-race": "16 线程高并发争用下 TokenBucket 原子补充与防超卖修复",
    "conc-02-async-flight-coalescer": "异步 SingleFlight 请求合并器及上游异常防卡死修复",
    "ui-01-billing-ledger-slop": "重构 AI 塑料感账单台账 HTML 至 4px 栅格与 Hallmark 零告警规范",
    "ui-02-incident-status-board": "构建零 sloplint 告警且支持等宽数字排版的区域故障状态看板",
    "refac-01-cron-expression-parser": "扩展 5 段式 Cron 解析器以支持步长区间、逗号列表与边界校验",
    "refac-02-jsonl-ring-compactor": "支持记录数与字节双上限且容忍损坏行的 JSONL 环形日志压缩器",
    "sec-01-dsn-credential-redactor": "结构化日志中的 PostgreSQL DSN 密码、Bearer 令牌与 sk-* 密钥脱敏",
    "sec-02-zip-slip-tar-extractor": "阻断 Tar 归档解压过程中的 Zip-Slip 路径穿越与符号链接逃逸",
}

ROLE_ZH: dict[str, str] = {
    "control": "对照组",
    "treatment": "实验组",
}


def localize_task_title(task_id: str, fallback_title: str, lang: str) -> str:
    if lang == "zh":
        return TASK_TITLE_ZH.get(task_id, fallback_title)
    return fallback_title


def localize_category(category: str, lang: str) -> str:
    if lang == "zh":
        return CATEGORY_ZH.get(category, category)
    return category
