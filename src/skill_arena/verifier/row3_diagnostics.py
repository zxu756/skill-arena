"""Row 3 repeatability comparison, evidence preservation, and JSON field diff diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

FLAKY_TARGET_TEST_NOTE = "差异只在被测仓库的测试结果里，可能是被测仓库的测试不稳定"

_MISSING: Any = object()


@dataclass(frozen=True, slots=True)
class JsonFieldDiff:
    """Single leaf-level difference between two parsed JSON verification payloads."""

    path: str
    run1_value: str
    run2_value: str


@dataclass(slots=True)
class Row3ComparisonResult:
    """Outcome of comparing two consecutive verification runs on the same repository."""

    passed: bool
    rc1: int | None
    rc2: int | None
    canonical_equal: bool
    prefix_equal: bool
    canonical_bytes_len: int
    canonical_sha256_prefix: str
    total_diff_count: int
    displayed_diffs: list[JsonFieldDiff] = field(default_factory=list)
    only_in_target_tests: bool = False
    evidence_dir: Path | None = None
    summary_message: str = ""
    diff_lines: list[str] = field(default_factory=list)


def truncate_diff_value(value: Any, max_chars: int = 160) -> str:
    """Render a JSON value onto a single line and truncate to at most `max_chars` characters."""
    if value is _MISSING:
        rendered = "<missing>"
    else:
        rendered = json.dumps(value, ensure_ascii=False)
    if len(rendered) > max_chars:
        if max_chars <= 3:
            return rendered[:max_chars]
        return rendered[: max_chars - 3] + "..."
    return rendered


def diff_json_values(
    left: Any,
    right: Any,
    *,
    max_value_chars: int = 160,
) -> list[JsonFieldDiff]:
    """Recursively collect leaf-level differences between `left` and `right` JSON structures."""
    diffs: list[JsonFieldDiff] = []

    def _walk(a: Any, b: Any, path: str) -> None:
        label = path or "<root>"
        if a is _MISSING or b is _MISSING:
            diffs.append(
                JsonFieldDiff(
                    path=label,
                    run1_value=truncate_diff_value(a, max_value_chars),
                    run2_value=truncate_diff_value(b, max_value_chars),
                )
            )
            return
        if type(a) is not type(b):
            diffs.append(
                JsonFieldDiff(
                    path=label,
                    run1_value=truncate_diff_value(a, max_value_chars),
                    run2_value=truncate_diff_value(b, max_value_chars),
                )
            )
            return
        if isinstance(a, dict):
            keys = list(dict.fromkeys([*a.keys(), *b.keys()]))
            for key in keys:
                child = f"{path}.{key}" if path else str(key)
                _walk(a.get(key, _MISSING), b.get(key, _MISSING), child)
            return
        if isinstance(a, list):
            limit = max(len(a), len(b))
            for idx in range(limit):
                child = f"{path}[{idx}]"
                x = a[idx] if idx < len(a) else _MISSING
                y = b[idx] if idx < len(b) else _MISSING
                _walk(x, y, child)
            return
        if a != b:
            diffs.append(
                JsonFieldDiff(
                    path=label,
                    run1_value=truncate_diff_value(a, max_value_chars),
                    run2_value=truncate_diff_value(b, max_value_chars),
                )
            )

    _walk(left, right, "")
    return diffs


def is_only_target_test_diffs(diffs: list[JsonFieldDiff]) -> bool:
    """Return True iff `diffs` is non-empty and every diff path is under `build_and_test.test`."""
    if not diffs:
        return False
    return all(
        d.path == "build_and_test.test"
        or d.path.startswith("build_and_test.test.")
        or d.path.startswith("build_and_test.test[")
        for d in diffs
    )


def resolve_default_evidence_dir(base_tmpdir: Path | str | None = None) -> Path:
    """Compute a non-colliding `${TMPDIR}/skill-arena-row3-<timestamp>` path without creating it yet."""
    raw_tmp = str(base_tmpdir) if base_tmpdir is not None else (os.environ.get("TMPDIR") or "/tmp")
    tmp_root = Path(raw_tmp).expanduser().absolute()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    cand = tmp_root / f"skill-arena-row3-{ts}"
    if not cand.exists():
        return cand
    cand_pid = tmp_root / f"skill-arena-row3-{ts}-{os.getpid()}"
    if not cand_pid.exists():
        return cand_pid
    seq = 1
    while True:
        cand_seq = tmp_root / f"skill-arena-row3-{ts}-{os.getpid()}-{seq}"
        if not cand_seq.exists():
            return cand_seq
        seq += 1


def _persist_evidence_files(
    *,
    evidence_dir: Path | None,
    run1_json: Path,
    run2_json: Path,
    run1_md: Path | None,
    run2_md: Path | None,
) -> Path:
    dest_dir = (
        evidence_dir.expanduser().absolute()
        if evidence_dir is not None
        else resolve_default_evidence_dir()
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    pairs: tuple[tuple[Path | None, str], ...] = (
        (run1_json, "run1.json"),
        (run2_json, "run2.json"),
        (run1_md, "run1.md"),
        (run2_md, "run2.md"),
    )
    for src, target_name in pairs:
        if src is not None and src.is_file():
            shutil.copy2(src, dest_dir / target_name)
    return dest_dir


def evaluate_row3_runs(
    *,
    rc1: int | None,
    rc2: int | None,
    run1_json: Path,
    run2_json: Path,
    run1_md: Path | None = None,
    run2_md: Path | None = None,
    evidence_dir: Path | None = None,
    max_diffs: int = 10,
    max_value_chars: int = 160,
) -> Row3ComparisonResult:
    """Evaluate Row 3 determinism, preserving JSON/Markdown evidence and field diffs on failure."""
    if not run1_json.is_file() or not run2_json.is_file():
        saved_dir = _persist_evidence_files(
            evidence_dir=evidence_dir,
            run1_json=run1_json,
            run2_json=run2_json,
            run1_md=run1_md,
            run2_md=run2_md,
        )
        return Row3ComparisonResult(
            passed=False,
            rc1=rc1,
            rc2=rc2,
            canonical_equal=False,
            prefix_equal=False,
            canonical_bytes_len=0,
            canonical_sha256_prefix="",
            total_diff_count=0,
            evidence_dir=saved_dir,
            summary_message=f"缺少两次运行 JSON 输出（rc1={rc1}, rc2={rc2}）；证据目录: {saved_dir}",
        )

    raw1 = run1_json.read_text(encoding="utf-8")
    raw2 = run2_json.read_text(encoding="utf-8")
    try:
        p1 = json.loads(raw1)
        p2 = json.loads(raw2)
    except json.JSONDecodeError as exc:
        saved_dir = _persist_evidence_files(
            evidence_dir=evidence_dir,
            run1_json=run1_json,
            run2_json=run2_json,
            run1_md=run1_md,
            run2_md=run2_md,
        )
        return Row3ComparisonResult(
            passed=False,
            rc1=rc1,
            rc2=rc2,
            canonical_equal=False,
            prefix_equal=False,
            canonical_bytes_len=0,
            canonical_sha256_prefix="",
            total_diff_count=0,
            evidence_dir=saved_dir,
            summary_message=f"两次运行 JSON 解析失败（rc1={rc1}, rc2={rc2}, err={str(exc)[:120]}）；证据目录: {saved_dir}",
        )

    if isinstance(p1, dict):
        p1.pop("timing_meta", None)
    if isinstance(p2, dict):
        p2.pop("timing_meta", None)

    b1 = (json.dumps(p1, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    b2 = (json.dumps(p2, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    canonical_equal = b1 == b2
    marker = '  "timing_meta":'
    prefix_same = (
        marker in raw1
        and marker in raw2
        and raw1.split(marker, 1)[0].encode("utf-8") == raw2.split(marker, 1)[0].encode("utf-8")
    )

    if rc1 == rc2 and canonical_equal and prefix_same:
        sha = hashlib.sha256(b1).hexdigest()[:16]
        return Row3ComparisonResult(
            passed=True,
            rc1=rc1,
            rc2=rc2,
            canonical_equal=True,
            prefix_equal=True,
            canonical_bytes_len=len(b1),
            canonical_sha256_prefix=sha,
            total_diff_count=0,
            evidence_dir=None,
            summary_message=(
                f"diy-harness 连续两次运行退出码一致（{rc1} == {rc2}），"
                f"两份 JSON 去掉 timing_meta 后逐字节相同（{len(b1)} 字节，sha256={sha}...）"
            ),
        )

    saved_dir = _persist_evidence_files(
        evidence_dir=evidence_dir,
        run1_json=run1_json,
        run2_json=run2_json,
        run1_md=run1_md,
        run2_md=run2_md,
    )
    all_diffs = diff_json_values(p1, p2, max_value_chars=max_value_chars)
    only_target_tests = is_only_target_test_diffs(all_diffs)
    shown_diffs = all_diffs[:max_diffs]

    msg_parts = [
        f"两次运行输出不一致（rc1={rc1}, rc2={rc2}, canonical_equal={canonical_equal}, prefix_equal={prefix_same}, diff_fields={len(all_diffs)}）"
    ]
    if only_target_tests:
        msg_parts.append(FLAKY_TARGET_TEST_NOTE)
    msg_parts.append(f"证据目录: {saved_dir}")

    diff_lines = [
        f"  [Row 3 diff {idx}/{len(all_diffs)}] {d.path}: run1={d.run1_value} | run2={d.run2_value}"
        for idx, d in enumerate(shown_diffs, start=1)
    ]

    return Row3ComparisonResult(
        passed=False,
        rc1=rc1,
        rc2=rc2,
        canonical_equal=canonical_equal,
        prefix_equal=prefix_same,
        canonical_bytes_len=len(b1),
        canonical_sha256_prefix="",
        total_diff_count=len(all_diffs),
        displayed_diffs=shown_diffs,
        only_in_target_tests=only_target_tests,
        evidence_dir=saved_dir,
        summary_message="；".join(msg_parts),
        diff_lines=diff_lines,
    )
