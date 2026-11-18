"""Convert a task pool directory (see the pool's FORMAT.md) into TaskSpec JSON.

Each task lives in ``<pool>/<task_id>/`` with ``task.json``, ``start/**`` (the agent's starting
files), ``tests/**`` (grading tests, mounted at ``tests/`` in the repository), ``reference/**`` (a
correct implementation overlaid on ``start``) and ``mutants/<name>/**`` (plausible but wrong
implementations). Every task becomes one or two TaskSpecs:

* ``visible``: the tests are seeded into the workspace and protected; the prompt names the test command.
* ``hidden``: the workspace has no tests; the grader overlays them onto the clean grading checkout.

Task ids get an ``@visible`` / ``@hidden`` suffix so both variants can share one experiment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skill_arena.models import TaskSpec

POOL_VARIANTS: tuple[str, ...] = ("visible", "hidden")
DEFAULT_POOL_TIMEOUT_SEC = 900

HIDDEN_TESTS_NOTE = (
    "No tests are provided. Your work will be graded by hidden tests that check every numbered rule in this "
    "specification."
)

_SKIP_PARTS = frozenset({"__pycache__", ".pytest_cache", ".DS_Store"})


def _read_tree(root: Path, prefix: str = "") -> dict[str, str]:
    """Return {repo-relative posix path: text} for every regular file under ``root``."""
    files: dict[str, str] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_PARTS for part in rel_parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        rel = "/".join(rel_parts)
        files[f"{prefix}{rel}"] = path.read_text(encoding="utf-8")
    return files


def visible_tests_note(test_command: str) -> str:
    return (
        f"Tests are provided under `tests/`. Run them with `{test_command}` from the repository root. "
        "Do not modify the provided test files; the grader checks them byte for byte."
    )


def base_task_id(task_id: str) -> str:
    return task_id.split("@", 1)[0]


def load_pool_task(task_dir: Path, variant: str, *, timeout_sec: int | None = None) -> TaskSpec:
    """Build one TaskSpec variant from ``task_dir``."""
    if variant not in POOL_VARIANTS:
        raise ValueError(f"Unknown pool variant {variant!r}; expected one of {POOL_VARIANTS}.")
    meta_path = task_dir / "task.json"
    try:
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {meta_path}: {exc}") from exc
    missing = {"task_id", "title", "prompt", "test_command"} - set(meta)
    if missing:
        raise ValueError(f"{meta_path} is missing {sorted(missing)}")

    start_files = _read_tree(task_dir / "start")
    if any(p == "tests" or p.startswith("tests/") for p in start_files):
        raise ValueError(f"{task_dir.name}: start/ must not contain tests/ (see FORMAT.md)")
    test_files = _read_tree(task_dir / "tests", prefix="tests/")
    if not test_files:
        raise ValueError(f"{task_dir.name}: no files under tests/")
    reference_files = _read_tree(task_dir / "reference")
    mutants_dir = task_dir / "mutants"
    mutant_files: list[dict[str, str]] = []
    if mutants_dir.is_dir():
        for mdir in sorted(p for p in mutants_dir.iterdir() if p.is_dir() and p.name not in _SKIP_PARTS):
            overlay = _read_tree(mdir)
            if overlay:
                mutant_files.append(overlay)

    prompt = str(meta["prompt"]).rstrip()
    test_command = str(meta["test_command"])
    if variant == "visible":
        seed_files = {**start_files, **test_files}
        protected = sorted(test_files)
        hidden: dict[str, str] = {}
        prompt = f"{prompt}\n\n{visible_tests_note(test_command)}\n"
    else:
        seed_files = dict(start_files)
        protected = []
        hidden = dict(test_files)
        prompt = f"{prompt}\n\n{HIDDEN_TESTS_NOTE}\n"

    eff_timeout = timeout_sec if timeout_sec is not None else int(meta.get("timeout_sec", DEFAULT_POOL_TIMEOUT_SEC))
    return TaskSpec(
        task_id=f"{meta['task_id']}@{variant}",
        title=str(meta["title"]),
        category=str(meta.get("category", "general")),
        prompt=prompt,
        test_command=test_command,
        protected_test_paths=protected,
        seed_files=seed_files,
        reference_solution_files=reference_files,
        max_diff_lines=int(meta.get("max_diff_lines", 120)),
        timeout_sec=int(eff_timeout),
        variant=variant,
        hidden_test_files=hidden,
        mutant_files=mutant_files,
    )


def export_pool(
    pool_dir: Path,
    variants: list[str],
    *,
    task_ids: list[str] | None = None,
    timeout_sec: int | None = None,
) -> list[TaskSpec]:
    """Load every task directory (or the selected ``task_ids``) in ``pool_dir`` for each variant."""
    pool_dir = Path(pool_dir)
    if not pool_dir.is_dir():
        raise ValueError(f"Pool directory not found: {pool_dir}")
    task_dirs = sorted(p for p in pool_dir.iterdir() if p.is_dir() and (p / "task.json").is_file())
    if task_ids:
        wanted = [base_task_id(t) for t in task_ids]
        by_name = {p.name: p for p in task_dirs}
        unknown = [t for t in wanted if t not in by_name]
        if unknown:
            raise ValueError(f"Unknown task id(s) in pool {pool_dir}: {', '.join(unknown)}")
        task_dirs = [by_name[t] for t in dict.fromkeys(wanted)]
    if not task_dirs:
        raise ValueError(f"No tasks (directories with task.json) found in {pool_dir}")
    specs: list[TaskSpec] = []
    for variant in variants:
        for task_dir in task_dirs:
            specs.append(load_pool_task(task_dir, variant, timeout_sec=timeout_sec))
    return specs
