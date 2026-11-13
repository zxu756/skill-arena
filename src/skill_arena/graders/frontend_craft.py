"""Deterministic Hallmark / sloplint craft grader for HTML & CSS outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


@dataclass(slots=True)
class FrontendCraftAudit:
    passed: bool
    fails: int
    warns: int
    findings: list[str]


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

    default_sloplint = sloplint_script or (Path.home() / ".agents/skills/hallmark/scripts/sloplint.mjs")
    node_bin = shutil.which("node")
    if node_bin and default_sloplint.is_file():
        proc = subprocess.run(
            [node_bin, str(default_sloplint), str(html_path), "--scope", "app"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        summary_match = re.search(r"(\d+)\s+FAIL,\s*(\d+)\s+REFLEX,\s*(\d+)\s+WARN", out)
        if summary_match:
            s_fail = int(summary_match.group(1)) + int(summary_match.group(2))
            s_warn = int(summary_match.group(3))
            if s_fail > 0 or s_warn > 0:
                fails = max(fails, s_fail)
                warns = max(warns, s_warn)
                findings.append(f"sloplint.mjs: {out.splitlines()[0]}")

    passed = fails == 0 and warns == 0
    return FrontendCraftAudit(passed=passed, fails=fails, warns=warns, findings=findings)
