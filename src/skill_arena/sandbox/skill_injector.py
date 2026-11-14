"""Deterministic SKILL.md and per-trial isolated HOME mounting for Control vs Treatment arms."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import shutil

from skill_arena.models import ContenderConfig


def _real_account_home() -> Path:
    """Return the OS account home directory via getpwuid without hardcoding host paths."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _remove_path(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def _stage_minimal_auth_files(host_home: Path, trial_home: Path) -> None:
    """Read-only copy minimal auth tokens into `trial_home` without skills/plugins/settings."""
    candidates = (
        (".claude/.credentials.json", ".claude/.credentials.json"),
        (".codex/auth.json", ".codex/auth.json"),
        (".vibe/.env", ".vibe/.env"),
    )
    for src_rel, dst_rel in candidates:
        src = host_home / src_rel
        if src.is_file() and not src.is_symlink():
            dst = trial_home / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass


@dataclass(slots=True)
class SkillMountReport:
    """Summary of skills and isolated HOME variables mounted into a trial sandbox."""

    mounted_skill_names: list[str]
    total_injected_chars: int
    estimated_tokens: int
    isolated_env_vars: dict[str, str]
    isolated_home: Path | None = None


class SkillInjector:
    """Mounts or strips target SKILL.md folders inside an isolated trial workspace and temporary HOME."""

    @staticmethod
    def apply(
        sandbox_root: Path,
        contender: ContenderConfig,
        trial_home: Path | None = None,
        *,
        auth_copy_mode: bool = False,
    ) -> SkillMountReport:
        if contender.strip_default_skills:
            for rel_skills in (
                ".agents/skills",
                ".claude/skills",
                ".codex/skills",
                ".opencode/skills",
            ):
                _remove_path(sandbox_root / rel_skills)

        if trial_home is not None:
            for rel_dir in (
                ".config",
                ".config/opencode/skills",
                ".local/share",
                ".cache",
                ".claude",
                ".claude/skills",
                ".codex",
                ".codex/skills",
                ".vibe",
                ".agents/skills",
            ):
                (trial_home / rel_dir).mkdir(parents=True, exist_ok=True)
            if auth_copy_mode or contender.auth_copy_mode:
                _stage_minimal_auth_files(_real_account_home(), trial_home)

        agents_skills_dir = sandbox_root / ".agents" / "skills"
        claude_skills_dir = sandbox_root / ".claude" / "skills"

        mounted_names: list[str] = []
        total_chars = 0
        skill_directives: list[str] = []

        for raw_skill_path in contender.skill_paths:
            src = Path(raw_skill_path).expanduser().resolve()
            if not src.exists():
                raise FileNotFoundError(f"Configured skill path does not exist: {src}")

            skill_name = src.stem if src.is_file() else src.name
            if skill_name.upper() == "SKILL":
                skill_name = src.parent.name

            for dest_parent in (agents_skills_dir, claude_skills_dir):
                dest = dest_parent / skill_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                _remove_path(dest)
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / "SKILL.md")

            if trial_home is not None:
                home_skill_dest = trial_home / ".agents" / "skills" / skill_name
                _remove_path(home_skill_dest)
                home_skill_dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, home_skill_dest)
                else:
                    home_skill_dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, home_skill_dest / "SKILL.md")

                rel_target = Path("../../.agents/skills") / skill_name
                for mirror_rel in (".claude/skills", ".codex/skills"):
                    mirror_link = trial_home / mirror_rel / skill_name
                    mirror_link.parent.mkdir(parents=True, exist_ok=True)
                    _remove_path(mirror_link)
                    mirror_link.symlink_to(rel_target)

            skill_file_to_measure = (src / "SKILL.md") if src.is_dir() else src
            if skill_file_to_measure.is_file():
                text = skill_file_to_measure.read_text(encoding="utf-8", errors="replace")
                total_chars += len(text)
            mounted_names.append(skill_name)
            skill_directives.append(
                f"- Mandatory skill `{skill_name}`: read `.agents/skills/{skill_name}/SKILL.md` and follow all rules before editing files."
            )

        instruction_blocks: list[str] = [
            "# SkillArena Workspace Contract",
            "",
            "- Modify implementation files to satisfy the task requirements and pass tests.",
            "- Do NOT modify or delete protected test files; test tampering fails anti-cheat verification.",
        ]
        if skill_directives:
            instruction_blocks.append("")
            instruction_blocks.append("## Active Benchmark Skills")
            instruction_blocks.extend(skill_directives)
        if contender.agents_md_snippet:
            instruction_blocks.append("")
            instruction_blocks.append("## Contender Instructions")
            instruction_blocks.append(contender.agents_md_snippet.strip())
            total_chars += len(contender.agents_md_snippet)

        rendered_instructions = "\n".join(instruction_blocks) + "\n"
        (sandbox_root / "AGENTS.md").write_text(rendered_instructions, encoding="utf-8")
        (sandbox_root / "CLAUDE.md").write_text(rendered_instructions, encoding="utf-8")

        env_vars: dict[str, str] = {
            "SKILL_ARENA_ACTIVE": "1",
            "SKILL_ARENA_CONTENDER": contender.contender_id,
            "SKILL_ARENA_SKILLS": ",".join(mounted_names),
        }
        if contender.strip_default_skills:
            env_vars["SKILL_ARENA_STRIPPED_GLOBAL_SKILLS"] = "1"
            env_vars["OPENCODE_DISABLE_GLOBAL_SKILLS"] = "1"

        if trial_home is not None:
            home_str = str(trial_home)
            env_vars.update(
                {
                    "HOME": home_str,
                    "XDG_CONFIG_HOME": f"{home_str}/.config",
                    "XDG_DATA_HOME": f"{home_str}/.local/share",
                    "XDG_CACHE_HOME": f"{home_str}/.cache",
                    "CLAUDE_CONFIG_DIR": f"{home_str}/.claude",
                    "CODEX_HOME": f"{home_str}/.codex",
                    "VIBE_HOME": f"{home_str}/.vibe",
                    "SKILL_ARENA_ISOLATED_HOME": "1",
                }
            )

        estimated_tokens = max(0, (total_chars + 3) // 4)
        return SkillMountReport(
            mounted_skill_names=mounted_names,
            total_injected_chars=total_chars,
            estimated_tokens=estimated_tokens,
            isolated_env_vars=env_vars,
            isolated_home=trial_home,
        )
