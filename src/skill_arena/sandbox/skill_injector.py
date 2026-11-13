"""Deterministic SKILL.md and AGENTS.md / CLAUDE.md mounting for Control vs Treatment arms."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from skill_arena.models import ContenderConfig


@dataclass(slots=True)
class SkillMountReport:
    """Summary of skills and instructions mounted into a trial sandbox."""

    mounted_skill_names: list[str]
    total_injected_chars: int
    estimated_tokens: int
    isolated_env_vars: dict[str, str]


class SkillInjector:
    """Mounts or strips target SKILL.md folders inside an isolated trial workspace."""

    @staticmethod
    def apply(sandbox_root: Path, contender: ContenderConfig) -> SkillMountReport:
        agents_skills_dir = sandbox_root / ".agents" / "skills"
        claude_skills_dir = sandbox_root / ".claude" / "skills"
        isolated_home = sandbox_root / ".skill-arena-env"
        isolated_home.mkdir(parents=True, exist_ok=True)

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
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                if src.is_dir():
                    shutil.copytree(src, dest)
                    skill_md = dest / "SKILL.md"
                else:
                    dest.mkdir(parents=True, exist_ok=True)
                    skill_md = dest / "SKILL.md"
                    shutil.copy2(src, skill_md)

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
            codex_home = isolated_home / "codex"
            codex_home.mkdir(parents=True, exist_ok=True)
            env_vars["SKILL_ARENA_STRIPPED_GLOBAL_SKILLS"] = "1"
            env_vars["OPENCODE_DISABLE_GLOBAL_SKILLS"] = "1"

        estimated_tokens = max(0, (total_chars + 3) // 4)
        return SkillMountReport(
            mounted_skill_names=mounted_names,
            total_injected_chars=total_chars,
            estimated_tokens=estimated_tokens,
            isolated_env_vars=env_vars,
        )
