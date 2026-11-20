"""Root-only README discovery, fenced shell block parsing, bilingual parity check, and command replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import tomllib
from typing import Any

from skill_arena.verifier.builder import summarize_command_output
from skill_arena.verifier.env_guard import run_verified_command

_ROOT_README_RE = re.compile(r"^readme(?:\.[a-z]{2}(?:[-_][a-z]{2,4})?)?\.md$", re.IGNORECASE)
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})([^\n`~]*)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+.+$")
_SHELL_FENCE_LANGS: frozenset[str] = frozenset({"bash", "sh", "zsh", "shell", "console"})

_LANG_FLAG_NORM_RE = re.compile(r"--lang(?:=|\s+)(?:en|zh|zh-CN|en-US)\b", re.IGNORECASE)

# Static classification regexes (project-agnostic)
_CREDENTIAL_ASSIGN_RE = re.compile(
    r"(?:^|[;\s]|export\s+)[A-Za-z0-9_]*(?:API_KEY|SECRET|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY)\s*=|"
    r"(?:^|[;\s])(?:source|\.)\s+(?:\./)?\.env\b|"
    r"\$\{[A-Za-z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*:\?[^}]*\}",
    re.IGNORECASE,
)
_COMMENT_NEEDS_KEY_RE = re.compile(
    r"(?:需要|需|requires?)\s+[A-Za-z0-9_]*API_KEY\b",
    re.IGNORECASE,
)
_COMMENT_NO_KEY_RE = re.compile(
    r"(?:不需要\s*(?:API\s*)?key|无需\s*(?:API\s*)?key|without\s+api\s+key|no\s+api\s+key)",
    re.IGNORECASE,
)
_ANGLE_PLACEHOLDER_RE = re.compile(r"(?<![0-9<&])<([A-Za-z][A-Za-z0-9_.-]*)>")
_ELLIPSIS_PLACEHOLDER_RE = re.compile(
    r"""(?:^|\s|=)(?:\.\.\.|…|"\.\.\."|'\.\.\.'|"…"|'…')(?=\s|$)"""
)
_PATH_PLACEHOLDER_RE = re.compile(
    r"(?:^|\s|=)(?:/path/to/[^\s\"']+|path/to/[^\s\"']+|~/(?:my-project|your-[^\s\"']+))",
    re.IGNORECASE,
)
_ONLINE_INSTALL_OR_FETCH_RE = re.compile(
    r"(?:^|&&|\|\||;)\s*(?:"
    r"uv\s+tool\s+install\b|"
    r"pipx\s+install\b|"
    r"brew\s+install\b|"
    r"apt(?:-get)?\s+install\b|"
    r"cargo\s+install\b|"
    r"go\s+install\b|"
    r"curl\s+https?://|"
    r"wget\s+https?://|"
    r"git\s+(?:fetch|pull)\b|"
    r"git\s+clone\s+(?:https?://|git@|ssh://)"
    r")",
    re.IGNORECASE,
)
_PIP_INSTALL_RE = re.compile(r"(?:^|&&|\|\||;)\s*(?:python3?\s+-m\s+)?pip3?\s+install\b", re.IGNORECASE)
_NODE_INSTALL_RE = re.compile(r"(?:^|&&|\|\||;)\s*(?:npm|pnpm|yarn)\s+install\b|(?:^|&&|\|\||;)\s*npx\s+", re.IGNORECASE)
_USER_HOME_PATH_RE = re.compile(r"(?:^|\s|=)(?:~/|\$HOME/|\$\{HOME\}/)")
_RELEASE_MUTATION_RE = re.compile(
    r"(?:^|&&|\|\||;)\s*(?:"
    r"git\s+(?:commit|tag|push)\b|"
    r"uv\s+version\s+--bump\b|"
    r"uv\s+publish\b|"
    r"npm\s+(?:version|publish)\b|"
    r"cargo\s+publish\b|"
    r"twine\s+upload\b|"
    r"gh\s+release\b"
    r")",
    re.IGNORECASE,
)
_DOCKER_CMD_RE = re.compile(
    r"(?:^|&&|\|\||;|\s)(?:"
    r"docker(?:\s+-|\s+[a-z])|"
    r"docker-compose\b|"
    r"--sandbox(?:=|\s+)docker\b|"
    r"[A-Za-z0-9_]*SANDBOX=docker\b|"
    r"\bsandbox\s+(?:build|gc|run|stop)\b"
    r")",
    re.IGNORECASE,
)
_REAL_AGENT_CMD_RE = re.compile(
    r"(?:--cli(?:=|\s+)(?:claude|codex|agy|vibe|opencode)\b|"
    r"(?:^|&&|\|\||;)\s*(?:claude|codex|agy|vibe|opencode)\b|"
    r"\b(?:RELAY_REAL_AGENTS|SKILL_ARENA_REAL_AB)=1\b)",
    re.IGNORECASE,
)
_DAEMON_CMD_RE = re.compile(
    r"(?:\bpty_daemon(?:\.py)?\b|"
    r"\btail\b[^\n]*\s-f\b|"
    r"\b(?:serve|studio|daemon)\b(?:\s+--|\s*$))",
    re.IGNORECASE,
)
_RECURSIVE_MILESTONE_RE = re.compile(
    r"(?:^|\s)(?:\./)?Scripts/test\.sh\s+(?:milestone|release)\b"
)
_TMP_IPC_PATH_RE = re.compile(r"(/tmp/[A-Za-z0-9_.-]+)")

# Post-execution deterministic missing-API-key diagnostic
_MISSING_API_KEY_OUTPUT_RE = re.compile(
    r"(?:缺少\s*API\s*key|"
    r"missing\s+api\s+key|"
    r"api[_\s-]?key\s+(?:is\s+)?(?:required|not\s+set|missing)|"
    r"requires?\s+(?:an?\s+)?[A-Za-z0-9_]*API_KEY|"
    r"需要\s+[A-Z0-9_]+_API_KEY|"
    r"please\s+set\s+[A-Z0-9_]+_API_KEY)",
    re.IGNORECASE,
)
_TEST_OR_DEV_CHECK_CMD_RE = re.compile(
    r"\b(?:pytest|unittest|ruff|mypy|flake8|swift\s+(?:test|build)|cargo\s+(?:test|build)|"
    r"sync_prompts|ci_sim|check_wheel|scorecard)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ReadmeCodeBlock:
    """One fenced code block inside a root README file."""

    doc_name: str
    block_index: int
    start_line: int
    end_line: int
    fence_lang: str
    section: str
    raw_body: str
    is_shell: bool


@dataclass(slots=True)
class ParsedReadmeCommand:
    """One logical shell command extracted from a README shell code block."""

    doc_name: str
    block_index: int
    line_no: int
    section: str
    command: str
    comment: str


@dataclass(slots=True)
class ReplayItem:
    """Verification verdict and evidence for a single README command."""

    index: int
    doc_name: str
    line_no: int
    section: str
    command: str
    status: str  # "PASS" | "FAIL" | "UNVERIFIABLE" | "SKIPPED"
    category: str
    exit_code: int | None
    reason: str
    output_summary: str = ""
    leaked_process_count: int = 0
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)
    reaped_pids: list[int] = field(default_factory=list)
    wall_ms: int = 0


@dataclass(slots=True)
class ReadmeReplaySummary:
    """Complete README parsing, bilingual parity, and command replay outcome."""

    status: str  # "PASS" | "FAIL"
    discovered_readmes: list[str]
    bilingual_parity_status: str  # "PASS" | "FAIL" | "N/A"
    bilingual_parity_reason: str
    fenced_block_count: int
    shell_block_count: int
    non_command_block_count: int
    skip_cmd_patterns: list[str]
    exempted_commands: list[str]
    items: list[ReplayItem]
    wall_ms: int = 0


def discover_root_readmes(repo_dir: Path) -> list[Path]:
    """Discover ONLY root-level README files (`README.md` and localized root siblings); never recurse."""
    candidates: list[Path] = []
    for child in repo_dir.iterdir():
        if child.is_file() and not child.is_symlink() and _ROOT_README_RE.match(child.name):
            candidates.append(child)

    def _sort_key(path: Path) -> tuple[int, str]:
        return (0 if path.name.lower() == "readme.md" else 1, path.name.lower())

    return sorted(candidates, key=_sort_key)


def discover_project_cli_names(repo_dir: Path) -> set[str]:
    """Read `[project.scripts]` from `pyproject.toml` (if any) to identify the repo's CLI entrypoints."""
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    scripts = data.get("project", {}).get("scripts", {})
    if isinstance(scripts, dict):
        return {str(k).strip() for k in scripts if str(k).strip()}
    return set()


def parse_readme_blocks(md_path: Path) -> list[ReadmeCodeBlock]:
    """Extract all fenced code blocks from `md_path` with exact 1-based line numbers and section headings."""
    lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[ReadmeCodeBlock] = []
    current_section = ""
    in_fence = False
    fence_marker = ""
    fence_lang = ""
    fence_start = 0
    body_lines: list[str] = []

    for line_no, raw_line in enumerate(lines, start=1):
        if not in_fence:
            if _HEADING_RE.match(raw_line):
                current_section = raw_line.strip()
                continue
            m_open = _FENCE_OPEN_RE.match(raw_line)
            if m_open:
                in_fence = True
                fence_marker = m_open.group(1)
                info = m_open.group(2).strip()
                fence_lang = info.split()[0].lower() if info else ""
                fence_start = line_no
                body_lines = []
        else:
            stripped = raw_line.strip()
            if (
                stripped.startswith(fence_marker[0] * len(fence_marker))
                and set(stripped) == {fence_marker[0]}
            ):
                blocks.append(
                    ReadmeCodeBlock(
                        doc_name=md_path.name,
                        block_index=len(blocks) + 1,
                        start_line=fence_start,
                        end_line=line_no,
                        fence_lang=fence_lang,
                        section=current_section,
                        raw_body="\n".join(body_lines),
                        is_shell=fence_lang in _SHELL_FENCE_LANGS,
                    )
                )
                in_fence = False
            else:
                body_lines.append(raw_line)

    return blocks


def _split_shell_comment(line: str) -> tuple[str, str]:
    """Split a shell line into `(code_part, comment_part)` respecting single/double quotes and escapes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip(), line[idx + 1 :].strip()
    return line.rstrip(), ""


def extract_commands_from_block(block: ReadmeCodeBlock) -> list[ParsedReadmeCommand]:
    """Extract ordered logical shell commands (joining `\\` line continuations) from a shell code block."""
    if not block.is_shell:
        return []

    commands: list[ParsedReadmeCommand] = []
    buf_parts: list[str] = []
    comment_parts: list[str] = []
    cmd_start_line = block.start_line + 1

    for offset, raw_line in enumerate(block.raw_body.splitlines(), start=1):
        abs_line = block.start_line + offset
        work_line = raw_line
        if block.fence_lang == "console":
            lstripped = work_line.lstrip()
            if not buf_parts:
                if not lstripped.startswith("$ "):
                    continue
                work_line = lstripped[2:]

        code_part, comment_part = _split_shell_comment(work_line)
        stripped_code = code_part.strip()

        if not buf_parts and not stripped_code:
            continue

        if not buf_parts:
            cmd_start_line = abs_line

        if comment_part:
            comment_parts.append(comment_part)

        if stripped_code.endswith("\\"):
            stem = stripped_code[:-1].rstrip()
            if stem:
                buf_parts.append(stem)
        else:
            if stripped_code:
                buf_parts.append(stripped_code)
            merged = re.sub(r"\s+", " ", " ".join(buf_parts)).strip()
            if merged:
                commands.append(
                    ParsedReadmeCommand(
                        doc_name=block.doc_name,
                        block_index=block.block_index,
                        line_no=cmd_start_line,
                        section=block.section,
                        command=merged,
                        comment=" ; ".join(comment_parts),
                    )
                )
            buf_parts = []
            comment_parts = []

    if buf_parts:
        merged = re.sub(r"\s+", " ", " ".join(buf_parts)).strip()
        if merged:
            commands.append(
                ParsedReadmeCommand(
                    doc_name=block.doc_name,
                    block_index=block.block_index,
                    line_no=cmd_start_line,
                    section=block.section,
                    command=merged,
                    comment=" ; ".join(comment_parts),
                )
            )
    return commands


def normalize_command_for_parity(cmd: str) -> str:
    """Normalize `--lang en` / `--lang zh` flags when checking bilingual README command parity."""
    return _LANG_FLAG_NORM_RE.sub("--lang <LANG>", cmd)


def check_bilingual_readme_parity(
    cmds_by_doc: dict[str, list[ParsedReadmeCommand]],
    *,
    lang: str = "zh",
) -> tuple[str, str]:
    """Compare normalized command sequences across root README files when multiple languages exist."""
    is_zh = lang == "zh"
    doc_names = list(cmds_by_doc.keys())
    if len(doc_names) <= 1:
        return (
            "N/A",
            "单语仓库（仅有一份根级 README）" if is_zh else "Single-language repository (only one root README)",
        )

    base_doc = doc_names[0]
    base_norm = [normalize_command_for_parity(c.command) for c in cmds_by_doc[base_doc]]

    for other_doc in doc_names[1:]:
        other_norm = [normalize_command_for_parity(c.command) for c in cmds_by_doc[other_doc]]
        if base_norm != other_norm:
            mismatch_detail = f"{base_doc} ({len(base_norm)}) vs {other_doc} ({len(other_norm)})"
            for idx, (lhs, rhs) in enumerate(zip(base_norm, other_norm), start=1):
                if lhs != rhs:
                    mismatch_detail += f"; first diff at #{idx}: {lhs!r} != {rhs!r}"
                    break
            return (
                "FAIL",
                (
                    f"双语 README 命令序列不一致：{mismatch_detail}"
                    if is_zh
                    else f"Bilingual README command parity mismatch: {mismatch_detail}"
                ),
            )

    return (
        "PASS",
        (
            f"已校验 {', '.join(doc_names)} 的 {len(base_norm)} 条命令序列一致"
            if is_zh
            else f"Verified command sequence parity ({len(base_norm)} commands) across {', '.join(doc_names)}"
        ),
    )


def _matches_skip_pattern(command: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if not pat:
            continue
        if pat in command:
            return pat
        try:
            if re.search(pat, command):
                return pat
        except re.error:
            continue
    return None


def _is_interactive_project_cli(command: str, cli_names: set[str]) -> bool:
    if not cli_names:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False

    # Strip leading KEY=VAL env assignments
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        idx += 1
    tokens = tokens[idx:]
    if not tokens:
        return False

    # Unwrap `uv run [options] <cli> ...`
    if tokens[0] == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        j = 2
        while j < len(tokens) and tokens[j].startswith("-"):
            j += 1
        tokens = tokens[j:]
        if not tokens:
            return False

    exe = Path(tokens[0]).name
    if exe not in cli_names:
        return False

    rest = tokens[1:]
    if not rest:
        return True
    # Session resume/continue TUI shortcuts (`-c`, `--continue`, `-r`, `--resume`)
    if rest[0] in {"-c", "--continue", "-r", "--resume"}:
        return True
    # `<cli> chat [--flags...]` with no prompt positional argument opens interactive REPL
    if rest[0] == "chat":
        positionals = [tok for tok in rest[1:] if not tok.startswith("-")]
        if not positionals:
            return True
    return False


def classify_command_static(
    parsed: ParsedReadmeCommand,
    *,
    repo_dir: Path,
    cli_names: set[str],
    skip_patterns: list[str],
    daemon_tmp_paths: set[str],
    lang: str = "zh",
) -> tuple[str, str] | None:
    """Return `(category, reason)` if `parsed` cannot/should not be executed offline, else `None`."""
    is_zh = lang == "zh"
    cmd = parsed.command

    matched_pat = _matches_skip_pattern(cmd, skip_patterns)
    if matched_pat is not None:
        return (
            "SKIPPED_USER",
            (
                f"已由 `--skip-cmd {matched_pat}` 显式豁免"
                if is_zh
                else f"Explicitly exempted via `--skip-cmd {matched_pat}`"
            ),
        )

    if _CREDENTIAL_ASSIGN_RE.search(cmd) or _COMMENT_NEEDS_KEY_RE.search(parsed.comment):
        return (
            "UNVERIFIABLE_CREDENTIALS",
            (
                "无法验证（需密钥）：命令设置或明确依赖外部 API Key / 密钥环境"
                if is_zh
                else "Unverifiable (credentials): command configures or explicitly requires an external API key"
            ),
        )

    for m_angle in _ANGLE_PLACEHOLDER_RE.finditer(cmd):
        inner = m_angle.group(1)
        if not (repo_dir / inner).exists():
            return (
                "UNVERIFIABLE_PLACEHOLDER",
                (
                    f"无法验证（含占位符）：命令包含示例占位参数 `<{inner}>`"
                    if is_zh
                    else f"Unverifiable (placeholder): command contains placeholder `<{inner}>`"
                ),
            )

    if _ELLIPSIS_PLACEHOLDER_RE.search(cmd) or _PATH_PLACEHOLDER_RE.search(cmd):
        return (
            "UNVERIFIABLE_PLACEHOLDER",
            (
                "无法验证（含占位符）：命令包含省略号或示例路径占位符"
                if is_zh
                else "Unverifiable (placeholder): command contains ellipsis or sample path placeholder"
            ),
        )

    if _ONLINE_INSTALL_OR_FETCH_RE.search(cmd):
        return (
            "UNVERIFIABLE_OFFLINE",
            (
                "无法验证（离线）：命令需联网或查询外部包索引（不读项目锁文件）"
                if is_zh
                else "Unverifiable (offline): command requires network or external package registry access"
            ),
        )

    if _PIP_INSTALL_RE.search(cmd) and "--no-index" not in cmd and "--find-links" not in cmd:
        return (
            "UNVERIFIABLE_OFFLINE",
            (
                "无法验证（离线）：`pip install` 未指定离线索引，需访问外部包源"
                if is_zh
                else "Unverifiable (offline): `pip install` requires online package index access"
            ),
        )

    if _NODE_INSTALL_RE.search(cmd) and "--offline" not in cmd:
        return (
            "UNVERIFIABLE_OFFLINE",
            (
                "无法验证（离线）：Node 包安装/拉取需访问外部注册表"
                if is_zh
                else "Unverifiable (offline): Node package install/fetch requires online registry access"
            ),
        )

    if _RELEASE_MUTATION_RE.search(cmd):
        return (
            "UNVERIFIABLE_RELEASE_MUTATION",
            (
                "无法验证（发版/提交推送）：改动版本号、提交历史或推送远端的发版命令不在回放中执行"
                if is_zh
                else "Unverifiable (release/git mutation): version bump, commit, tag, or remote push command"
            ),
        )

    if _USER_HOME_PATH_RE.search(cmd):
        return (
            "UNVERIFIABLE_USER_HOME",
            (
                "无法验证（写用户目录）：命令直接读写用户家目录 `~/` 路径"
                if is_zh
                else "Unverifiable (user home): command directly accesses or mutates `~/` user home path"
            ),
        )

    if _DOCKER_CMD_RE.search(cmd):
        return (
            "UNVERIFIABLE_DOCKER",
            (
                "无法验证（需容器服务）：依赖运行中的 Docker 容器守护进程"
                if is_zh
                else "Unverifiable (Docker): requires a running Docker container daemon"
            ),
        )

    if _REAL_AGENT_CMD_RE.search(cmd):
        return (
            "UNVERIFIABLE_REAL_AGENT",
            (
                "无法验证（需付费 Agent CLI）：离线验收禁止调用真实外部 Agent 命令行"
                if is_zh
                else "Unverifiable (real agent CLI): invoking live paid coding agents is prohibited in offline verification"
            ),
        )

    if _RECURSIVE_MILESTONE_RE.search(cmd):
        return (
            "UNVERIFIABLE_DAEMON",
            (
                "无法验证（防递归编排）：顶层里程碑脚本自身调用 README 回放，跳过以防递归"
                if is_zh
                else "Unverifiable (recursive orchestrator): top-level milestone script invokes README replay itself"
            ),
        )

    if _DAEMON_CMD_RE.search(cmd):
        for m_tmp in _TMP_IPC_PATH_RE.finditer(cmd):
            daemon_tmp_paths.add(m_tmp.group(1))
        return (
            "UNVERIFIABLE_DAEMON",
            (
                "无法验证（常驻服务）：命令启动常驻监听服务或守护进程"
                if is_zh
                else "Unverifiable (daemon/server): command starts a long-running server or daemon"
            ),
        )

    if daemon_tmp_paths and any(p in cmd for p in daemon_tmp_paths):
        return (
            "UNVERIFIABLE_DAEMON",
            (
                "无法验证（常驻服务配套命令）：依赖同代码块中已跳过的常驻服务管道/日志文件"
                if is_zh
                else "Unverifiable (daemon companion): interacts with IPC pipe/log of a skipped daemon in the same block"
            ),
        )

    if _is_interactive_project_cli(cmd, cli_names):
        return (
            "UNVERIFIABLE_INTERACTIVE",
            (
                "无法验证（交互式会话）：命令进入交互式终端 REPL/TUI 会话"
                if is_zh
                else "Unverifiable (interactive): command launches an interactive TUI/REPL session"
            ),
        )

    return None


def replay_root_readmes(
    repo_dir: Path,
    env: dict[str, str],
    *,
    skip_patterns: list[str] | None = None,
    timeout_sec: int = 300,
    lang: str = "zh",
) -> ReadmeReplaySummary:
    """Discover root READMEs, validate bilingual command parity, and classify/replay every shell command."""
    is_zh = lang == "zh"
    eff_skip = list(skip_patterns or [])
    readmes = discover_root_readmes(repo_dir)
    if not readmes:
        return ReadmeReplaySummary(
            status="FAIL",
            discovered_readmes=[],
            bilingual_parity_status="N/A",
            bilingual_parity_reason="未找到根级 README.md" if is_zh else "No root README.md found",
            fenced_block_count=0,
            shell_block_count=0,
            non_command_block_count=0,
            skip_cmd_patterns=eff_skip,
            exempted_commands=[],
            items=[],
            wall_ms=0,
        )

    cli_names = discover_project_cli_names(repo_dir)
    all_blocks: list[ReadmeCodeBlock] = []
    cmds_by_doc: dict[str, list[ParsedReadmeCommand]] = {}

    for md_path in readmes:
        blocks = parse_readme_blocks(md_path)
        all_blocks.extend(blocks)
        doc_cmds: list[ParsedReadmeCommand] = []
        for blk in blocks:
            doc_cmds.extend(extract_commands_from_block(blk))
        cmds_by_doc[md_path.name] = doc_cmds

    parity_status, parity_reason = check_bilingual_readme_parity(cmds_by_doc, lang=lang)

    # Refresh run_env: if `.venv` exists in `repo_dir`, do not pollute PYTHONPATH; else include `src/` if present
    run_env = dict(env)
    if not (repo_dir / ".venv").exists():
        py_paths = [str(repo_dir)]
        if (repo_dir / "src").is_dir():
            py_paths.insert(0, str(repo_dir / "src"))
        run_env["PYTHONPATH"] = ":".join(py_paths)

    items: list[ReplayItem] = []
    exempted_cmds: list[str] = []
    executed_primary_cmds: set[str] = set()
    total_wall_ms = 0
    primary_doc = readmes[0].name

    for md_path in readmes:
        doc_name = md_path.name
        current_block_idx: int | None = None
        block_cwd = repo_dir
        daemon_tmp_paths: set[str] = set()

        for parsed in cmds_by_doc[doc_name]:
            if parsed.block_index != current_block_idx:
                current_block_idx = parsed.block_index
                block_cwd = repo_dir
                daemon_tmp_paths = set()

            item_idx = len(items) + 1
            static_verdict = classify_command_static(
                parsed,
                repo_dir=repo_dir,
                cli_names=cli_names,
                skip_patterns=eff_skip,
                daemon_tmp_paths=daemon_tmp_paths,
                lang=lang,
            )
            if static_verdict is not None:
                cat, reason = static_verdict
                if cat == "SKIPPED_USER":
                    st = "SKIPPED"
                    exempted_cmds.append(f"{doc_name}:L{parsed.line_no}: {parsed.command}")
                else:
                    st = "UNVERIFIABLE"
                items.append(
                    ReplayItem(
                        index=item_idx,
                        doc_name=doc_name,
                        line_no=parsed.line_no,
                        section=parsed.section,
                        command=parsed.command,
                        status=st,
                        category=cat,
                        exit_code=None,
                        reason=reason,
                    )
                )
                continue

            # Deduplicate byte-identical command in secondary README when already executed in primary README
            if doc_name != primary_doc and parsed.command in executed_primary_cmds:
                items.append(
                    ReplayItem(
                        index=item_idx,
                        doc_name=doc_name,
                        line_no=parsed.line_no,
                        section=parsed.section,
                        command=parsed.command,
                        status="SKIPPED",
                        category="SKIPPED_PARITY_DUPLICATE",
                        exit_code=None,
                        reason=(
                            f"与主文档 `{primary_doc}` 中已执行通过的命令逐字相同，跳过重复执行"
                            if is_zh
                            else f"Byte-identical command already executed in `{primary_doc}`"
                        ),
                    )
                )
                continue

            # Track simple intra-block `cd <subdir>` when subdir stays inside `repo_dir`
            if parsed.command.startswith("cd ") and all(sep not in parsed.command for sep in ("&&", ";", "|")):
                target_raw = parsed.command[3:].strip().strip("'\"")
                candidate_cwd = (block_cwd / target_raw).resolve()
                if candidate_cwd.is_dir() and candidate_cwd.is_relative_to(repo_dir.resolve()):
                    block_cwd = candidate_cwd
                    items.append(
                        ReplayItem(
                            index=item_idx,
                            doc_name=doc_name,
                            line_no=parsed.line_no,
                            section=parsed.section,
                            command=parsed.command,
                            status="PASS",
                            category="EXECUTED_PASS",
                            exit_code=0,
                            reason="切换工作区内子目录成功" if is_zh else "Changed working directory within repo",
                        )
                    )
                    if doc_name == primary_doc:
                        executed_primary_cmds.add(parsed.command)
                    continue

            res = run_verified_command(
                ["/bin/bash", "-c", parsed.command],
                cwd=block_cwd,
                env=run_env,
                timeout_sec=timeout_sec,
            )
            if (repo_dir / ".venv").exists():
                run_env.pop("PYTHONPATH", None)
            total_wall_ms += res.wall_ms
            combined = res.combined_output
            out_summary = summarize_command_output(combined)
            leaked_n = len(res.leaked_processes)

            # Tier 2: Deterministic post-execution missing-API-key diagnostic (only when NOT a dev/test check
            # and NOT claiming "no API key required")
            if (
                res.returncode != 0
                and not res.timed_out
                and leaked_n == 0
                and not _COMMENT_NO_KEY_RE.search(parsed.comment)
                and not _TEST_OR_DEV_CHECK_CMD_RE.search(parsed.command)
                and _MISSING_API_KEY_OUTPUT_RE.search(combined)
            ):
                first_line = next((ln.strip() for ln in combined.splitlines() if ln.strip()), "")
                items.append(
                    ReplayItem(
                        index=item_idx,
                        doc_name=doc_name,
                        line_no=parsed.line_no,
                        section=parsed.section,
                        command=parsed.command,
                        status="UNVERIFIABLE",
                        category="UNVERIFIABLE_CREDENTIALS",
                        exit_code=res.returncode,
                        reason=(
                            f"无法验证（需密钥）：无密钥沙箱实测提示需外部 API Key（{first_line[:120]}）"
                            if is_zh
                            else f"Unverifiable (credentials): command reported missing external API key ({first_line[:120]})"
                        ),
                        output_summary=out_summary,
                        wall_ms=res.wall_ms,
                    )
                )
                continue

            if res.timed_out:
                item_status = "FAIL"
                item_cat = "EXECUTED_FAIL"
                item_reason = f"命令超时（>{timeout_sec}s）" if is_zh else f"Command timed out (>{timeout_sec}s)"
            elif res.returncode != 0:
                item_status = "FAIL"
                item_cat = "EXECUTED_FAIL"
                item_reason = (
                    f"干净拷贝离线回放失败（退出码 {res.returncode}）"
                    if is_zh
                    else f"Failed in clean offline clone (exit code {res.returncode})"
                )
            elif leaked_n > 0:
                item_status = "FAIL"
                item_cat = "EXECUTED_FAIL"
                item_reason = (
                    f"命令退出码为 0 但遗留 {leaked_n} 个后台进程（已强杀回收）"
                    if is_zh
                    else f"Command exited 0 but leaked {leaked_n} background process(es) (reaped)"
                )
            else:
                item_status = "PASS"
                item_cat = "EXECUTED_PASS"
                item_reason = "离线回放通过（退出码 0）" if is_zh else "Offline replay passed (exit code 0)"
                if doc_name == primary_doc:
                    executed_primary_cmds.add(parsed.command)

            items.append(
                ReplayItem(
                    index=item_idx,
                    doc_name=doc_name,
                    line_no=parsed.line_no,
                    section=parsed.section,
                    command=parsed.command,
                    status=item_status,
                    category=item_cat,
                    exit_code=res.returncode,
                    reason=item_reason,
                    output_summary=out_summary,
                    leaked_process_count=leaked_n,
                    leaked_processes=res.leaked_processes,
                    reaped_pids=res.reaped_pids,
                    wall_ms=res.wall_ms,
                )
            )

    shell_blocks = sum(1 for b in all_blocks if b.is_shell)
    non_cmd_blocks = len(all_blocks) - shell_blocks
    any_item_failed = any(i.status == "FAIL" for i in items)
    overall_status = "FAIL" if (parity_status == "FAIL" or any_item_failed) else "PASS"

    return ReadmeReplaySummary(
        status=overall_status,
        discovered_readmes=[p.name for p in readmes],
        bilingual_parity_status=parity_status,
        bilingual_parity_reason=parity_reason,
        fenced_block_count=len(all_blocks),
        shell_block_count=shell_blocks,
        non_command_block_count=non_cmd_blocks,
        skip_cmd_patterns=eff_skip,
        exempted_commands=exempted_cmds,
        items=items,
        wall_ms=total_wall_ms,
    )
