"""Deterministic single-point mutation verification for Python (AST) and Swift (line-level)."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any

from skill_arena.verifier.builder import (
    detect_build_and_test_commands,
    detect_zero_tests_ran,
    summarize_command_output,
)
from skill_arena.verifier.env_guard import (
    CommandRunResult,
    _make_tree_writable,
    run_verified_command,
)

_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "build",
        "dist",
        ".build",
        "node_modules",
        "site-packages",
        "tests",
        "test",
        "Tests",
        "Test",
        "testing",
        "evals",
        "evals-redteam",
        "benchmarks",
        "docs",
        "examples",
        "scripts",
        "Scripts",
        "spike",
        "fixtures",
        "testdata",
    }
)

_EXCLUDED_PY_FILENAMES: frozenset[str] = frozenset(
    {
        "setup.py",
        "conftest.py",
        "__main__.py",
    }
)

_LOGGING_RECEIVERS: frozenset[str] = frozenset({"logging", "logger", "log", "warnings"})

_SYNTAX_OR_IMPORT_ERROR_RE = re.compile(
    r"(?:"
    r"\bSyntaxError\b|"
    r"\bIndentationError\b|"
    r"\bTabError\b|"
    r"\bImportError\b|"
    r"\bModuleNotFoundError\b|"
    r"ERROR\s+collecting\s+|"
    r"Interrupted:\s+\d+\s+errors?\s+during\s+collection|"
    r"ImportError\s+while\s+importing\s+test\s+module|"
    r"unittest\.loader\._FailedTest"
    r")"
)

_PYTEST_FAILED_COUNT_RE = re.compile(r"\b([1-9]\d*)\s+failed\b")
_PYTEST_ERROR_COUNT_RE = re.compile(r"\b([1-9]\d*)\s+errors?\b")
_PYTEST_ASSERTION_MARKER_RE = re.compile(
    r"(?:\bAssertionError\b|^\s*E\s+assert\b|\bFailed:\s+DID\s+NOT\s+RAISE\b|\bFailed:\s+)",
    re.MULTILINE,
)

_UNITTEST_ASSERTION_MARKER_RE = re.compile(
    r"(?:^FAIL:\s+|\bAssertionError\b|^FAILED\s+\([^)]*\bfailures=[1-9]\d*)",
    re.MULTILINE,
)
_UNITTEST_PURE_ERRORS_RE = re.compile(r"^FAILED\s+\(errors=[1-9]\d*\)\s*$", re.MULTILINE)

_SWIFT_COMPILER_ERR_RE = re.compile(r":\d+:\d+:\s+error:\s+")
_SWIFT_TEST_FAILURE_RE = re.compile(
    r"(?:✘\s+Test\b|\bExpectation\s+failed\b|\bIssue\s+recorded\b|"
    r"\bXCTAssert[A-Za-z]*\s+failed\b|Test\s+Case\s+.*?\s+failed\b|"
    r"Test\s+run\s+with\s+\d+\s+tests?.*?\bfailed\b|"
    r"Executed\s+\d+\s+tests?,\s+with\s+[1-9]\d*\s+failures?)",
    re.IGNORECASE,
)

_SWIFT_LINE_OPS: tuple[tuple[str, str, str], ...] = (
    (" == ", " != ", "compare_eq_to_neq"),
    (" != ", " == ", "compare_neq_to_eq"),
    (" <= ", " > ", "compare_lte_to_gt"),
    (" >= ", " < ", "compare_gte_to_lt"),
    (" + ", " - ", "arith_add_to_sub"),
    (" - ", " + ", "arith_sub_to_add"),
    ("return true", "return false", "return_bool_flip"),
    ("return false", "return true", "return_bool_flip"),
)


@dataclass(frozen=True, slots=True)
class MutationCandidate:
    """One deterministic single-point source mutation candidate."""

    language: str  # "python" | "swift"
    rel_path: str
    line_no: int
    col_offset: int
    operator: str
    description: str
    mutated_source: str


@dataclass(slots=True)
class MutationRecord:
    """Execution outcome of evaluating a single mutation candidate."""

    index: int
    language: str
    file: str
    line_no: int
    col_offset: int
    operator: str
    description: str
    test_command: str
    verdict: str  # "KILLED" | "SURVIVED" | "INVALID"
    mutated_exit_code: int | None
    mutated_output_summary: str
    restored_tests_passed: bool
    restored_exit_code: int | None
    worktree_clean: bool
    leaked_process_count: int = 0
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)
    reaped_pids: list[int] = field(default_factory=list)
    reason: str = ""
    wall_ms: int = 0


@dataclass(slots=True)
class MutationSummary:
    """Aggregated outcome of destructive mutation verification."""

    enabled: bool
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    max_mutations: int
    evaluated_count: int
    killed_count: int
    survived_count: int
    invalid_skipped_count: int
    reason: str
    items: list[MutationRecord] = field(default_factory=list)
    wall_ms: int = 0


def classify_mutation_test_run(
    test_command: str,
    exit_code: int,
    combined_output: str,
    *,
    timed_out: bool = False,
    compile_failed: bool = False,
    lang: str = "zh",
) -> tuple[str, str]:
    """Classify mutated test execution into `("KILLED" | "SURVIVED" | "INVALID", reason)`.

    Rules:
      - `KILLED`: test runner executed tests cleanly and exited non-zero with a genuine assertion failure.
      - `SURVIVED`: test runner executed >0 tests and exited 0 (mutation went undetected).
      - `INVALID`: syntax error, indentation error, import/module error, compilation failure,
        pytest collection/usage/internal error (exit codes 2/3/4/5), 0 tests ran, timeout, or non-assertion crash.
    """
    is_zh = lang == "zh"
    if compile_failed:
        return (
            "INVALID",
            "编译失败（不计入杀死）" if is_zh else "Compilation failed (not counted as killed)",
        )
    if timed_out:
        return (
            "INVALID",
            "变异后测试超时（非断言失败，不计入杀死）"
            if is_zh
            else "Mutated test run timed out (not an assertion failure)",
        )

    cmd_lower = test_command.lower()

    if _SYNTAX_OR_IMPORT_ERROR_RE.search(combined_output):
        return (
            "INVALID",
            "触发语法错误、导入错误或测试收集错误（不计入杀死）"
            if is_zh
            else "Triggered syntax, import, or test collection error (not counted as killed)",
        )

    if "pytest" in cmd_lower and exit_code in {2, 3, 4, 5}:
        return (
            "INVALID",
            f"pytest 返回非断言退出码 {exit_code}（收集/内部/用法/0用例错误，不计入杀死）"
            if is_zh
            else f"pytest exited with code {exit_code} (collection/internal/usage/no-tests error, not killed)",
        )

    if detect_zero_tests_ran(test_command, exit_code, combined_output):
        return (
            "INVALID",
            "变异后运行了 0 条测试（不计入杀死）" if is_zh else "0 tests ran under mutation (not counted as killed)",
        )

    if exit_code == 0:
        return (
            "SURVIVED",
            "变异存活：修改实现后测试仍全部通过（退出码 0）"
            if is_zh
            else "Mutation survived: tests still exited 0",
        )

    if "pytest" in cmd_lower:
        if exit_code != 1:
            return (
                "INVALID",
                f"pytest 退出码 {exit_code} 非断言失败退出码 1（不计入杀死）"
                if is_zh
                else f"pytest exit code {exit_code} != 1 (not an assertion failure)",
            )
        has_failed_summary = _PYTEST_FAILED_COUNT_RE.search(combined_output) is not None
        has_assertion = _PYTEST_ASSERTION_MARKER_RE.search(combined_output) is not None
        has_errors_only = (
            _PYTEST_ERROR_COUNT_RE.search(combined_output) is not None and not has_failed_summary
        )
        if has_errors_only or not (has_failed_summary and has_assertion):
            return (
                "INVALID",
                "pytest 非断言异常退出（无 AssertionError，不计入杀死）"
                if is_zh
                else "pytest failed without AssertionError (runtime/setup error, not counted as killed)",
            )
        return (
            "KILLED",
            "变异被杀死：pytest 以断言失败（AssertionError，退出码 1）退出"
            if is_zh
            else "Mutation killed: pytest exited 1 with AssertionError",
        )

    if "unittest" in cmd_lower:
        if _UNITTEST_PURE_ERRORS_RE.search(combined_output) or not _UNITTEST_ASSERTION_MARKER_RE.search(
            combined_output
        ):
            return (
                "INVALID",
                "unittest 以非断言错误（ERROR）退出，不计入杀死"
                if is_zh
                else "unittest exited with runtime/import ERROR rather than assertion FAIL",
            )
        return (
            "KILLED",
            "变异被杀死：unittest 以断言失败（FAIL / AssertionError）退出"
            if is_zh
            else "Mutation killed: unittest failed with assertion failure",
        )

    if "swift test" in cmd_lower:
        if _SWIFT_COMPILER_ERR_RE.search(combined_output) and not _SWIFT_TEST_FAILURE_RE.search(combined_output):
            return (
                "INVALID",
                "Swift 测试目标编译失败（不计入杀死）"
                if is_zh
                else "Swift test target failed to compile (not counted as killed)",
            )
        if _SWIFT_TEST_FAILURE_RE.search(combined_output):
            return (
                "KILLED",
                "变异被杀死：swift test 以测试断言失败退出"
                if is_zh
                else "Mutation killed: swift test reported assertion failure",
            )
        return (
            "INVALID",
            "swift test 非断言错误退出（不计入杀死）"
            if is_zh
            else "swift test exited non-zero without assertion failure marker",
        )

    # Custom test command override
    if re.search(r"(?:\bAssertionError\b|\bFAIL\b|\bfailed\b)", combined_output):
        return (
            "KILLED",
            f"变异被杀死：测试以断言失败退出（退出码 {exit_code}）"
            if is_zh
            else f"Mutation killed: test command failed with assertion error (exit {exit_code})",
        )
    return (
        "INVALID",
        f"测试命令退出码 {exit_code} 未见断言失败标记（不计入杀死）"
        if is_zh
        else f"Test command exited {exit_code} without assertion failure evidence",
    )


def _is_excluded_rel_path(rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts[:-1]:
        if part in _EXCLUDED_DIR_NAMES or part.startswith("."):
            return True
    fname = rel_parts[-1]
    if fname.startswith("."):
        return True
    return False


def _collect_non_test_python_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo_dir.rglob("*.py")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo_dir)
        if _is_excluded_rel_path(rel.parts):
            continue
        fname = path.name
        if fname in _EXCLUDED_PY_FILENAMES:
            continue
        if fname.startswith("test_") or fname.endswith("_test.py"):
            continue
        files.append(path)
    return files


def _collect_non_test_swift_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo_dir.rglob("*.swift")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo_dir)
        if _is_excluded_rel_path(rel.parts):
            continue
        fname = path.name
        if fname == "Package.swift" or fname.endswith("Tests.swift") or fname.startswith("Test"):
            continue
        files.append(path)
    return files


def _replace_ast_span(source: str, node: ast.AST, replacement_expr: str) -> str | None:
    """Replace exact UTF-8 byte span `(lineno, col_offset)..(end_lineno, end_col_offset)` of `node`."""
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    col_offset = getattr(node, "col_offset", None)
    end_col_offset = getattr(node, "end_col_offset", None)
    if (
        lineno is None
        or end_lineno is None
        or col_offset is None
        or end_col_offset is None
        or lineno < 1
        or end_lineno < lineno
    ):
        return None

    raw_lines = source.splitlines(keepends=True)
    if end_lineno > len(raw_lines):
        return None

    start_line_bytes = raw_lines[lineno - 1].encode("utf-8")
    end_line_bytes = raw_lines[end_lineno - 1].encode("utf-8")
    if col_offset > len(start_line_bytes) or end_col_offset > len(end_line_bytes):
        return None

    prefix = "".join(raw_lines[: lineno - 1]) + start_line_bytes[:col_offset].decode("utf-8", errors="strict")
    suffix = end_line_bytes[end_col_offset:].decode("utf-8", errors="strict") + "".join(raw_lines[end_lineno:])
    mutated = f"{prefix}({replacement_expr}){suffix}" if not replacement_expr.startswith("return") else f"{prefix}{replacement_expr}{suffix}"
    if mutated == source:
        return None
    try:
        ast.parse(mutated)
    except SyntaxError:
        # Retry without outer parens (e.g. statement-level replacement)
        mutated_plain = f"{prefix}{replacement_expr}{suffix}"
        if mutated_plain == source:
            return None
        try:
            ast.parse(mutated_plain)
            return mutated_plain
        except SyntaxError:
            return None
    return mutated


def _is_string_or_seq_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.JoinedStr, ast.List, ast.Tuple, ast.Dict, ast.Set)):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return True
    return False


def _is_none_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_logging_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in _LOGGING_RECEIVERS:
            return True
    return False


def _return_annotation_kind(fn_node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str | None:
    if fn_node is None or fn_node.returns is None:
        return None
    try:
        ann = ast.unparse(fn_node.returns).strip()
    except Exception:
        return None
    # Skip optional union returns (`T | None` / `Optional[T]`) when picking typed dummy constants
    if "None" in ann or "Optional" in ann:
        return None
    ann_lower = ann.lower()
    if ann == "bool":
        return "bool"
    if ann == "int":
        return "int"
    if ann == "str":
        return "str"
    if ann_lower.startswith(("list", "sequence", "iterable")):
        return "list"
    if ann_lower.startswith(("dict", "mapping")):
        return "dict"
    return None


def _python_candidates_for_file(repo_dir: Path, py_path: Path) -> list[MutationCandidate]:
    rel_path = py_path.relative_to(repo_dir).as_posix()
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel_path)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    raw_candidates: list[tuple[int, int, int, MutationCandidate]] = []
    occupied_lines: set[int] = set()

    func_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name not in {"__repr__", "__str__"}
    ]
    func_nodes.sort(key=lambda n: (n.lineno, n.col_offset))

    def _visit_stmt_block(
        stmts: list[ast.stmt],
        enclosing_fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
        *,
        is_top_level_script: bool = False,
    ) -> None:
        for idx, stmt in enumerate(stmts):
            # Skip module/function docstring
            if (
                idx == 0
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            # Skip `if __name__ == "__main__":` and `if TYPE_CHECKING:`
            if isinstance(stmt, ast.If):
                try:
                    cond_txt = ast.unparse(stmt.test)
                except Exception:
                    cond_txt = ""
                if "__name__" in cond_txt or "TYPE_CHECKING" in cond_txt:
                    continue
            if isinstance(stmt, ast.Expr) and _is_logging_call(stmt.value):
                continue
            # Do not descend into nested class/function definitions here (functions are iterated explicitly)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue

            _scan_node(stmt, enclosing_fn, is_top_level_script=is_top_level_script)

    def _add_candidate(
        node: ast.AST,
        op_rank: int,
        operator: str,
        description: str,
        mutated_src: str | None,
    ) -> None:
        if mutated_src is None:
            return
        line_no = getattr(node, "lineno", 0)
        col_offset = getattr(node, "col_offset", 0)
        cand = MutationCandidate(
            language="python",
            rel_path=rel_path,
            line_no=line_no,
            col_offset=col_offset,
            operator=operator,
            description=description,
            mutated_source=mutated_src,
        )
        raw_candidates.append((line_no, col_offset, op_rank, cand))
        occupied_lines.add(line_no)

    def _scan_node(
        node: ast.AST,
        enclosing_fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
        *,
        is_top_level_script: bool = False,
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if _is_logging_call(node):
            return

        # 1. Single-operator comparison negation
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            op = node.ops[0]
            comp = node.comparators[0]
            op_map: dict[type[ast.cmpop], tuple[ast.cmpop, str, str]] = {
                ast.Eq: (ast.NotEq(), "compare_negate", "`==` -> `!=`"),
                ast.NotEq: (ast.Eq(), "compare_negate", "`!=` -> `==`"),
                ast.Lt: (ast.GtE(), "compare_negate", "`<` -> `>=`"),
                ast.GtE: (ast.Lt(), "compare_negate", "`>=` -> `<`"),
                ast.LtE: (ast.Gt(), "compare_negate", "`<=` -> `>`"),
                ast.Gt: (ast.LtE(), "compare_negate", "`>` -> `<=`"),
                ast.In: (ast.NotIn(), "compare_negate", "`in` -> `not in`"),
                ast.NotIn: (ast.In(), "compare_negate", "`not in` -> `in`"),
            }
            if type(op) in op_map and not (_is_none_constant(comp) or _is_none_constant(node.left)):
                new_op, op_name, desc = op_map[type(op)]
                cloned = copy.copy(node)
                cloned.ops = [new_op]
                repl = ast.unparse(cloned)
                _add_candidate(node, 1, op_name, desc, _replace_ast_span(source, node, repl))

        # 2. Binary arithmetic swap (`+` <-> `-`)
        elif isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add) and not (
                _is_string_or_seq_literal(node.left) or _is_string_or_seq_literal(node.right)
            ):
                cloned = copy.copy(node)
                cloned.op = ast.Sub()
                _add_candidate(
                    node,
                    2,
                    "arith_swap",
                    "`+` -> `-`",
                    _replace_ast_span(source, node, ast.unparse(cloned)),
                )
            elif isinstance(node.op, ast.Sub) and not (
                isinstance(node.left, ast.Set) or isinstance(node.right, ast.Set)
            ):
                cloned = copy.copy(node)
                cloned.op = ast.Add()
                _add_candidate(
                    node,
                    2,
                    "arith_swap",
                    "`-` -> `+`",
                    _replace_ast_span(source, node, ast.unparse(cloned)),
                )

        # 3. Boolean logic swap (`and` <-> `or`)
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                cloned = copy.copy(node)
                cloned.op = ast.Or()
                _add_candidate(
                    node,
                    3,
                    "bool_swap",
                    "`and` -> `or`",
                    _replace_ast_span(source, node, ast.unparse(cloned)),
                )
            elif isinstance(node.op, ast.Or):
                cloned = copy.copy(node)
                cloned.op = ast.And()
                _add_candidate(
                    node,
                    3,
                    "bool_swap",
                    "`or` -> `and`",
                    _replace_ast_span(source, node, ast.unparse(cloned)),
                )

        # 4. Unary `not x` -> `x`
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            _add_candidate(
                node,
                4,
                "unary_not_drop",
                "`not expr` -> `expr`",
                _replace_ast_span(source, node, ast.unparse(node.operand)),
            )

        # Recurse children first so inner expressions on a `return` line register before `Return` fallback
        for child in ast.iter_child_nodes(node):
            # Skip function/variable type annotations
            if isinstance(node, ast.AnnAssign) and child is node.annotation:
                continue
            if isinstance(node, ast.arg) and child is node.annotation:
                continue
            _scan_node(child, enclosing_fn, is_top_level_script=is_top_level_script)

        # 5. Return value constant / negation mutation
        if isinstance(node, ast.Return) and node.value is not None:
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, bool):
                flipped = "False" if val.value else "True"
                _add_candidate(
                    val,
                    5,
                    "return_const",
                    f"`return {val.value}` -> `return {flipped}`",
                    _replace_ast_span(source, val, flipped),
                )
            elif isinstance(val, ast.Constant) and isinstance(val.value, int) and not isinstance(val.value, bool):
                new_int = "1" if val.value == 0 else "0"
                _add_candidate(
                    val,
                    5,
                    "return_const",
                    f"`return {val.value}` -> `return {new_int}`",
                    _replace_ast_span(source, val, new_int),
                )
            elif isinstance(val, ast.Constant) and isinstance(val.value, str):
                new_str = '""' if val.value != "" else '"__mutated__"'
                _add_candidate(
                    val,
                    5,
                    "return_const",
                    f"`return {val.value!r}` -> `return {new_str}`",
                    _replace_ast_span(source, val, new_str),
                )
            elif node.lineno not in occupied_lines:
                ret_kind = _return_annotation_kind(enclosing_fn)
                val_src = ast.unparse(val)
                if ret_kind == "bool" or (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Attribute)
                    and val.func.attr in {"startswith", "endswith", "isdigit", "isalpha"}
                ):
                    _add_candidate(
                        val,
                        6,
                        "return_negate",
                        f"`return {val_src[:40]}` -> `return not (...)`",
                        _replace_ast_span(source, val, f"not ({val_src})"),
                    )
                elif ret_kind == "int":
                    _add_candidate(
                        val,
                        6,
                        "return_const",
                        f"`return {val_src[:40]}` -> `return -999999`",
                        _replace_ast_span(source, val, "-999999"),
                    )
                elif ret_kind == "str":
                    _add_candidate(
                        val,
                        6,
                        "return_const",
                        f"`return {val_src[:40]}` -> `return '__mutated__'`",
                        _replace_ast_span(source, val, '"__mutated__"'),
                    )
                elif ret_kind == "list":
                    _add_candidate(
                        val,
                        6,
                        "return_const",
                        f"`return {val_src[:40]}` -> `return []`",
                        _replace_ast_span(source, val, "[]"),
                    )
                elif ret_kind == "dict":
                    _add_candidate(
                        val,
                        6,
                        "return_const",
                        f"`return {val_src[:40]}` -> `return {{}}`",
                        _replace_ast_span(source, val, "{}"),
                    )

        # 6. Top-level script print literal fallback (when script has no functions)
        if (
            is_top_level_script
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and node.args
        ):
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str) and arg0.value:
                _add_candidate(
                    arg0,
                    7,
                    "script_output_const",
                    f"`print({arg0.value!r})` -> `print('__mutated__')`",
                    _replace_ast_span(source, arg0, '"__mutated__"'),
                )

    if func_nodes:
        for fn in func_nodes:
            _visit_stmt_block(fn.body, fn, is_top_level_script=False)
    else:
        _visit_stmt_block(tree.body, None, is_top_level_script=True)

    raw_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    deduped: list[MutationCandidate] = []
    seen_pos: set[tuple[int, int]] = set()
    for line_no, col_offset, _rank, cand in raw_candidates:
        pos = (line_no, col_offset)
        if pos not in seen_pos:
            seen_pos.add(pos)
            deduped.append(cand)
    return deduped


def _swift_candidates_for_file(repo_dir: Path, swift_path: Path) -> list[MutationCandidate]:
    rel_path = swift_path.relative_to(repo_dir).as_posix()
    try:
        source = swift_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    raw_lines = source.splitlines(keepends=True)
    candidates: list[tuple[int, int, MutationCandidate]] = []
    in_block_comment = False

    for idx, raw_line in enumerate(raw_lines):
        line_no = idx + 1
        stripped = raw_line.strip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if not stripped or stripped.startswith(("//", "import ", "@available", "#if", "#else", "#endif")):
            continue

        code_only = raw_line.split("//", 1)[0]
        for old_tok, new_tok, op_name in _SWIFT_LINE_OPS:
            col = code_only.find(old_tok)
            if col >= 0:
                mutated_line = code_only[:col] + new_tok + raw_line[col + len(old_tok) :]
                mutated_full = "".join(raw_lines[:idx] + [mutated_line] + raw_lines[idx + 1 :])
                if mutated_full != source:
                    candidates.append(
                        (
                            line_no,
                            col,
                            MutationCandidate(
                                language="swift",
                                rel_path=rel_path,
                                line_no=line_no,
                                col_offset=col,
                                operator=op_name,
                                description=f"`{old_tok.strip()}` -> `{new_tok.strip()}`",
                                mutated_source=mutated_full,
                            ),
                        )
                    )
                    break

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [c for _, _, c in candidates]


def discover_mutation_candidates(repo_dir: Path) -> list[MutationCandidate]:
    """Discover all single-point mutation candidates sorted deterministically by `(rel_path, line_no, col_offset)`."""
    all_candidates: list[MutationCandidate] = []
    for py_file in _collect_non_test_python_files(repo_dir):
        all_candidates.extend(_python_candidates_for_file(repo_dir, py_file))
    for swift_file in _collect_non_test_swift_files(repo_dir):
        all_candidates.extend(_swift_candidates_for_file(repo_dir, swift_file))
    all_candidates.sort(key=lambda c: (c.rel_path, c.line_no, c.col_offset))
    return all_candidates


def _find_matching_python_test_file(repo_dir: Path, src_rel_path: str) -> str | None:
    stem = Path(src_rel_path).stem
    if not stem or stem == "__init__":
        return None
    expected_names = {f"test_{stem}.py", f"{stem}_test.py"}
    for test_root in ("tests", "test"):
        root = repo_dir / test_root
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.py")):
            if candidate.name in expected_names:
                return candidate.relative_to(repo_dir).as_posix()
    return None


def _prepare_clean_worktree_for_mutation(repo_dir: Path, env: dict[str, str]) -> None:
    """Restore any tracked edits or stray untracked files left by README replay while keeping `.venv`/`.build`."""
    _make_tree_writable(repo_dir)
    info_exclude = repo_dir / ".git" / "info" / "exclude"
    if info_exclude.parent.is_dir():
        existing = info_exclude.read_text(encoding="utf-8", errors="replace") if info_exclude.is_file() else ""
        needed = [pat for pat in (".venv/", ".build/", ".pytest_cache/", "node_modules/") if pat not in existing]
        if needed:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            info_exclude.write_text(existing + sep + "\n".join(needed) + "\n", encoding="utf-8")
    _ = run_verified_command(["git", "checkout", "--", "."], cwd=repo_dir, env=env, timeout_sec=30)
    _ = run_verified_command(
        [
            "git",
            "clean",
            "-fd",
            "-e",
            ".venv",
            "-e",
            ".build",
            "-e",
            ".pytest_cache",
            "-e",
            "node_modules",
        ],
        cwd=repo_dir,
        env=env,
        timeout_sec=30,
    )


def _check_worktree_clean(repo_dir: Path, env: dict[str, str]) -> bool:
    st_res = run_verified_command(["git", "status", "--porcelain"], cwd=repo_dir, env=env, timeout_sec=30)
    return st_res.returncode == 0 and st_res.stdout.strip() == ""


def evaluate_mutation_candidate(
    repo_dir: Path,
    candidate: MutationCandidate,
    test_cmd: str,
    env: dict[str, str],
    *,
    index: int = 1,
    allow_targeted_pytest: bool = False,
    swift_build_cmd: str | None = None,
    timeout_sec: int = 300,
    lang: str = "zh",
) -> MutationRecord:
    """Apply `candidate` in `repo_dir`, run build/test check, restore exact bytes, and verify clean worktree."""
    is_zh = lang == "zh"
    target_path = repo_dir / candidate.rel_path
    orig_bytes = target_path.read_bytes()

    run_env = dict(env)
    if not (repo_dir / ".venv").exists():
        py_paths = [str(repo_dir)]
        if (repo_dir / "src").is_dir():
            py_paths.insert(0, str(repo_dir / "src"))
        run_env["PYTHONPATH"] = ":".join(py_paths)

    t0 = time.monotonic()
    leaked_all: list[dict[str, Any]] = []
    reaped_all: list[int] = []
    used_test_cmd = test_cmd
    verdict = "INVALID"
    reason = ""
    mut_exit: int | None = None
    mut_summary = ""
    restored_ok = False
    restored_exit: int | None = None

    try:
        target_path.write_bytes(candidate.mutated_source.encode("utf-8"))

        # 1. Python syntax gate: never treat SyntaxError as KILLED
        if candidate.language == "python":
            try:
                compile(candidate.mutated_source, candidate.rel_path, "exec")
            except SyntaxError as exc:
                verdict = "INVALID"
                reason = (
                    f"变异产生 Python 语法错误（{exc.msg}），不计入杀死"
                    if is_zh
                    else f"Mutation produced Python SyntaxError ({exc.msg}); not counted as killed"
                )
                mut_summary = f"SyntaxError: {exc}"
                return MutationRecord(
                    index=index,
                    language=candidate.language,
                    file=candidate.rel_path,
                    line_no=candidate.line_no,
                    col_offset=candidate.col_offset,
                    operator=candidate.operator,
                    description=candidate.description,
                    test_command=used_test_cmd,
                    verdict=verdict,
                    mutated_exit_code=None,
                    mutated_output_summary=mut_summary,
                    restored_tests_passed=True,
                    restored_exit_code=0,
                    worktree_clean=True,
                    reason=reason,
                    wall_ms=int(round((time.monotonic() - t0) * 1000)),
                )

        # 2. Swift build gate: build first; compile failures do not count as KILLED
        if candidate.language == "swift":
            b_cmd = swift_build_cmd or "swift build --disable-sandbox"
            b_res: CommandRunResult = run_verified_command(
                ["/bin/bash", "-c", b_cmd],
                cwd=repo_dir,
                env=run_env,
                timeout_sec=timeout_sec,
            )
            leaked_all.extend(b_res.leaked_processes)
            reaped_all.extend(b_res.reaped_pids)
            if b_res.returncode != 0 or b_res.timed_out:
                verdict, reason = classify_mutation_test_run(
                    test_cmd,
                    b_res.returncode,
                    b_res.combined_output,
                    timed_out=b_res.timed_out,
                    compile_failed=True,
                    lang=lang,
                )
                mut_exit = b_res.returncode
                mut_summary = summarize_command_output(b_res.combined_output)
                return MutationRecord(
                    index=index,
                    language=candidate.language,
                    file=candidate.rel_path,
                    line_no=candidate.line_no,
                    col_offset=candidate.col_offset,
                    operator=candidate.operator,
                    description=candidate.description,
                    test_command=b_cmd,
                    verdict=verdict,
                    mutated_exit_code=mut_exit,
                    mutated_output_summary=mut_summary,
                    restored_tests_passed=True,
                    restored_exit_code=0,
                    worktree_clean=True,
                    leaked_process_count=len(leaked_all),
                    leaked_processes=leaked_all,
                    reaped_pids=reaped_all,
                    reason=reason,
                    wall_ms=int(round((time.monotonic() - t0) * 1000)),
                )

        # 3. Execute tests (fast targeted unit test first if available, falling back to full suite on survival)
        targeted_rel = (
            _find_matching_python_test_file(repo_dir, candidate.rel_path)
            if (allow_targeted_pytest and candidate.language == "python")
            else None
        )
        if targeted_rel is not None:
            targeted_cmd = f"{test_cmd} {targeted_rel} -q"
            t_res = run_verified_command(
                ["/bin/bash", "-c", targeted_cmd],
                cwd=repo_dir,
                env=run_env,
                timeout_sec=timeout_sec,
            )
            leaked_all.extend(t_res.leaked_processes)
            reaped_all.extend(t_res.reaped_pids)
            v_targ, r_targ = classify_mutation_test_run(
                targeted_cmd,
                t_res.returncode,
                t_res.combined_output,
                timed_out=t_res.timed_out,
                lang=lang,
            )
            if v_targ == "KILLED":
                used_test_cmd = targeted_cmd
                verdict = v_targ
                reason = r_targ
                mut_exit = t_res.returncode
                mut_summary = summarize_command_output(t_res.combined_output)
            else:
                # Confirm against full test suite before declaring SURVIVED or INVALID
                full_res = run_verified_command(
                    ["/bin/bash", "-c", test_cmd],
                    cwd=repo_dir,
                    env=run_env,
                    timeout_sec=timeout_sec,
                )
                leaked_all.extend(full_res.leaked_processes)
                reaped_all.extend(full_res.reaped_pids)
                used_test_cmd = test_cmd
                verdict, reason = classify_mutation_test_run(
                    test_cmd,
                    full_res.returncode,
                    full_res.combined_output,
                    timed_out=full_res.timed_out,
                    lang=lang,
                )
                mut_exit = full_res.returncode
                mut_summary = summarize_command_output(full_res.combined_output)
        else:
            full_res = run_verified_command(
                ["/bin/bash", "-c", test_cmd],
                cwd=repo_dir,
                env=run_env,
                timeout_sec=timeout_sec,
            )
            leaked_all.extend(full_res.leaked_processes)
            reaped_all.extend(full_res.reaped_pids)
            verdict, reason = classify_mutation_test_run(
                test_cmd,
                full_res.returncode,
                full_res.combined_output,
                timed_out=full_res.timed_out,
                lang=lang,
            )
            mut_exit = full_res.returncode
            mut_summary = summarize_command_output(full_res.combined_output)
    finally:
        target_path.write_bytes(orig_bytes)

    # Clean any untracked temp artifacts tests may have written, then verify restored tests & clean git status
    _prepare_clean_worktree_for_mutation(repo_dir, run_env)

    if verdict in {"KILLED", "SURVIVED"}:
        res_restore = run_verified_command(
            ["/bin/bash", "-c", used_test_cmd],
            cwd=repo_dir,
            env=run_env,
            timeout_sec=timeout_sec,
        )
        leaked_all.extend(res_restore.leaked_processes)
        reaped_all.extend(res_restore.reaped_pids)
        restored_exit = res_restore.returncode
        restored_ok = (
            not res_restore.timed_out
            and res_restore.returncode == 0
            and not detect_zero_tests_ran(used_test_cmd, res_restore.returncode, res_restore.combined_output)
        )
        _prepare_clean_worktree_for_mutation(repo_dir, run_env)
    else:
        restored_ok = True
        restored_exit = 0

    clean_after = _check_worktree_clean(repo_dir, run_env)
    wall_ms = int(round((time.monotonic() - t0) * 1000))

    return MutationRecord(
        index=index,
        language=candidate.language,
        file=candidate.rel_path,
        line_no=candidate.line_no,
        col_offset=candidate.col_offset,
        operator=candidate.operator,
        description=candidate.description,
        test_command=used_test_cmd,
        verdict=verdict,
        mutated_exit_code=mut_exit,
        mutated_output_summary=mut_summary,
        restored_tests_passed=restored_ok,
        restored_exit_code=restored_exit,
        worktree_clean=clean_after,
        leaked_process_count=len(leaked_all),
        leaked_processes=leaked_all,
        reaped_pids=reaped_all,
        reason=reason,
        wall_ms=wall_ms,
    )


def run_mutation_verification(
    repo_dir: Path,
    env: dict[str, str],
    *,
    max_mutations: int = 1,
    build_cmd_override: str | None = None,
    test_cmd_override: str | None = None,
    timeout_sec: int = 300,
    lang: str = "zh",
) -> MutationSummary:
    """Run up to `max_mutations` deterministic single-point mutations in `(path, line, col)` order."""
    is_zh = lang == "zh"
    t0 = time.monotonic()

    auto_build, auto_test = detect_build_and_test_commands(repo_dir)
    eff_build = build_cmd_override.strip() if build_cmd_override is not None else auto_build
    eff_test = test_cmd_override.strip() if test_cmd_override is not None else auto_test

    if not eff_test:
        return MutationSummary(
            enabled=True,
            status="FAIL",
            max_mutations=max_mutations,
            evaluated_count=0,
            killed_count=0,
            survived_count=0,
            invalid_skipped_count=0,
            reason="无法确定测试命令，无法执行变异反证" if is_zh else "No test command available for mutation verification",
            items=[],
            wall_ms=0,
        )

    _prepare_clean_worktree_for_mutation(repo_dir, env)
    candidates = discover_mutation_candidates(repo_dir)
    if not candidates:
        return MutationSummary(
            enabled=True,
            status="FAIL",
            max_mutations=max_mutations,
            evaluated_count=0,
            killed_count=0,
            survived_count=0,
            invalid_skipped_count=0,
            reason=(
                "未在非测试源码中找到可变异的 Python AST 或 Swift 候选点"
                if is_zh
                else "No single-point Python AST or Swift mutation candidates found in non-test sources"
            ),
            items=[],
            wall_ms=int(round((time.monotonic() - t0) * 1000)),
        )

    allow_targeted_pytest = test_cmd_override is None and eff_test in {"pytest", "uv run pytest"}
    valid_records: list[MutationRecord] = []
    invalid_records: list[MutationRecord] = []
    max_invalid_attempts = max(12, max_mutations * 8)

    for cand in candidates:
        if len(valid_records) >= max_mutations:
            break
        if len(invalid_records) >= max_invalid_attempts:
            break

        rec = evaluate_mutation_candidate(
            repo_dir,
            cand,
            eff_test,
            env,
            index=len(valid_records) + 1,
            allow_targeted_pytest=allow_targeted_pytest,
            swift_build_cmd=eff_build if cand.language == "swift" else None,
            timeout_sec=timeout_sec,
            lang=lang,
        )
        if rec.verdict == "INVALID":
            invalid_records.append(rec)
            continue
        valid_records.append(rec)

    wall_ms = int(round((time.monotonic() - t0) * 1000))
    killed_n = sum(1 for r in valid_records if r.verdict == "KILLED")
    survived_n = sum(1 for r in valid_records if r.verdict == "SURVIVED")
    all_restored_clean = all(r.restored_tests_passed and r.worktree_clean and r.leaked_process_count == 0 for r in valid_records)

    if not valid_records:
        detail = invalid_records[0].reason if invalid_records else ""
        reason = (
            f"共尝试 {len(invalid_records)} 处候选均触发语法/导入/编译/收集错误，0 处被断言有效杀死（{detail}）"
            if is_zh
            else f"All {len(invalid_records)} attempted candidate(s) hit syntax/import/compile/collection errors; 0 killed ({detail})"
        )
        return MutationSummary(
            enabled=True,
            status="FAIL",
            max_mutations=max_mutations,
            evaluated_count=0,
            killed_count=0,
            survived_count=0,
            invalid_skipped_count=len(invalid_records),
            reason=reason,
            items=invalid_records[: max_mutations],
            wall_ms=wall_ms,
        )

    if survived_n > 0 or not all_restored_clean:
        status = "FAIL"
        if survived_n > 0:
            reason = (
                f"执行 {len(valid_records)} 处有效变异：杀死 {killed_n} 处，存活 {survived_n} 处（判失败）"
                if is_zh
                else f"Evaluated {len(valid_records)} valid mutation(s): {killed_n} killed, {survived_n} survived (FAIL)"
            )
        else:
            reason = (
                "变异还原后测试未通过、工作树不干净或存在残留进程"
                if is_zh
                else "Post-mutation restore check failed (tests failed, dirty worktree, or leaked process)"
            )
    else:
        status = "PASS"
        reason = (
            f"执行 {len(valid_records)} 处单点变异全部被断言失败杀死，还原后测试全过且工作树干净"
            if is_zh
            else f"All {len(valid_records)} single-point mutation(s) killed by assertion failure; restored tests passed & worktree clean"
        )

    return MutationSummary(
        enabled=True,
        status=status,
        max_mutations=max_mutations,
        evaluated_count=len(valid_records),
        killed_count=killed_n,
        survived_count=survived_n,
        invalid_skipped_count=len(invalid_records),
        reason=reason,
        items=valid_records,
        wall_ms=wall_ms,
    )
