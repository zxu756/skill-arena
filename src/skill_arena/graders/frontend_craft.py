"""Deterministic Hallmark / sloplint craft grader for HTML & CSS outputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

VENDORED_SLOPLINT = Path(__file__).resolve().parent / "vendor" / "hallmark" / "sloplint.mjs"
VENDORED_SLOPLINT_VERSION = "hallmark v2.0.0 (04977e1)"
SLOPLINT_ENV_VAR = "SKILL_ARENA_SLOPLINT"
SLOPLINT_TIMEOUT_SEC = 120


@dataclass(slots=True)
class FrontendCraftAudit:
    passed: bool
    fails: int
    warns: int
    findings: list[str]
    sloplint_source: str = ""


def resolve_sloplint_script(explicit: Path | None = None) -> tuple[Path, str]:
    """Pick the sloplint.mjs to run: explicit path, then $SKILL_ARENA_SLOPLINT, then the vendored copy."""
    if explicit is not None:
        return Path(explicit), "explicit"
    env_val = os.environ.get(SLOPLINT_ENV_VAR, "").strip()
    if env_val:
        return Path(env_val).expanduser(), f"env {SLOPLINT_ENV_VAR}"
    return VENDORED_SLOPLINT, f"vendored {VENDORED_SLOPLINT_VERSION}"


def _sha256_prefix(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "unreadable"


_EM_OR_EN_DASH_RE = re.compile(r"[\u2014\u2013]")
_HEADING_SPAN_RE = re.compile(r"<h[12][^>]*>(?:(?!</h[12]>).)*<span\b", re.IGNORECASE | re.DOTALL)
_SPACING_PROP_RE = re.compile(
    r"\b(?:padding(?:-(?:top|right|bottom|left))?|margin(?:-(?:top|right|bottom|left))?|gap|row-gap|column-gap)\s*:\s*([^;\}]+)",
    re.IGNORECASE,
)
_PX_VALUE_RE = re.compile(r"(-?\d+(?:\.\d+)?)px\b", re.IGNORECASE)
_BANNED_DARK_SLOP_RE = re.compile(r"#(?:0b0e14|0d1117|0f172a)\b", re.IGNORECASE)


def audit_html_craft(html_path: Path, sloplint_script: Path | None = None) -> FrontendCraftAudit:
    """Audit an HTML artifact using built-in Hallmark rules plus optional sloplint.mjs."""
    if not html_path.is_file():
        return FrontendCraftAudit(passed=False, fails=1, warns=0, findings=[f"Missing target HTML: {html_path.name}"])

    raw = html_path.read_text(encoding="utf-8", errors="replace")
    fails = 0
    warns = 0
    findings: list[str] = []

    # Strip <script> and <style> blocks when checking prose copy for em/en-dashes
    prose_only = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    prose_text = re.sub(r"<[^>]+>", " ", prose_only)
    if _EM_OR_EN_DASH_RE.search(prose_text):
        fails += 1
        findings.append("FAIL [copy-dash]: Em-dash or en-dash found in visible copy or <title>.")

    if _HEADING_SPAN_RE.search(raw):
        fails += 1
        findings.append("FAIL [heading-span]: Nested <span> inside <h1>/<h2> splits heading tone.")

    if _BANNED_DARK_SLOP_RE.search(raw):
        fails += 1
        findings.append("FAIL [slop-palette]: Generic dark-navy template hex (#0b0e14/#0f172a) detected.")

    if not re.search(r'<meta\s+name=["\']description["\']', raw, re.IGNORECASE):
        warns += 1
        findings.append("WARN [meta-description]: Missing <meta name=\"description\"> tag.")

    if "text-wrap: balance" not in raw and "text-wrap:balance" not in raw:
        warns += 1
        findings.append("WARN [heading-wrap]: Missing text-wrap: balance on headings.")

    for match in _SPACING_PROP_RE.finditer(raw):
        val_expr = match.group(1)
        for px_match in _PX_VALUE_RE.finditer(val_expr):
            px = float(px_match.group(1))
            if px != 0 and (px % 4.0) != 0:
                fails += 1
                findings.append(f"FAIL [off-4px-grid]: Off-scale spacing value {px_match.group(0)} in '{match.group(0).strip()}'.")
                break

    script, origin = resolve_sloplint_script(sloplint_script)
    node_bin = shutil.which("node")
    if not script.is_file():
        source = f"not run: {origin} {script} is missing (built-in rules only)"
    elif not node_bin:
        source = f"not run: node not found (built-in rules only); would use {origin} {script}"
    else:
        source = f"{origin} {script} sha256:{_sha256_prefix(script)}"
        try:
            proc = subprocess.run(
                [node_bin, str(script), str(html_path), "--scope", "app"],
                capture_output=True,
                text=True,
                check=False,
                timeout=SLOPLINT_TIMEOUT_SEC,
            )
            out = (proc.stdout + "\n" + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            out = ""
            source += f" (timed out after {SLOPLINT_TIMEOUT_SEC}s)"
        summary_match = re.search(r"(\d+)\s+FAIL,\s*(\d+)\s+REFLEX,\s*(\d+)\s+WARN", out)
        if summary_match:
            s_fail = int(summary_match.group(1)) + int(summary_match.group(2))
            s_warn = int(summary_match.group(3))
            if s_fail > 0 or s_warn > 0:
                fails = max(fails, s_fail)
                warns = max(warns, s_warn)
                findings.append(f"sloplint.mjs: {out.splitlines()[0]}")
                findings.extend(f"sloplint.mjs: {line}" for line in out.splitlines()[1:] if line.strip())

    passed = fails == 0 and warns == 0
    return FrontendCraftAudit(passed=passed, fails=fails, warns=warns, findings=findings, sloplint_source=source)
