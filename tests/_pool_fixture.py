"""A tiny task pool in the FORMAT.md layout, used by the pool, hidden-test and mutation tests."""

from __future__ import annotations

import json
from pathlib import Path

CALC_REFERENCE = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def clamp(x, lo, hi):\n"
    "    if lo > hi:\n"
    "        raise ValueError('lo > hi')\n"
    "    return max(lo, min(x, hi))\n"
)
CALC_START = (
    "def add(a, b):\n"
    "    raise NotImplementedError\n"
    "\n"
    "\n"
    "def clamp(x, lo, hi):\n"
    "    raise NotImplementedError\n"
)
CALC_MUTANTS = {
    "subtracts": CALC_REFERENCE.replace("return a + b", "return a - b"),
    "no-raise": CALC_REFERENCE.replace("        raise ValueError('lo > hi')\n", "        pass\n"),
    "ignores-hi": CALC_REFERENCE.replace("return max(lo, min(x, hi))", "return max(lo, x)"),
}
CALC_TESTS = (
    "import unittest\n"
    "\n"
    "from calc import add, clamp\n"
    "from tests.calc_cases import ADD_CASES\n"
    "\n"
    "\n"
    "class CalcTests(unittest.TestCase):\n"
    "    # rule 1\n"
    "    def test_add(self):\n"
    "        for a, b, want in ADD_CASES:\n"
    "            self.assertEqual(add(a, b), want)\n"
    "\n"
    "    # rule 2\n"
    "    def test_clamp(self):\n"
    "        self.assertEqual(clamp(5, 0, 3), 3)\n"
    "        self.assertEqual(clamp(-1, 0, 3), 0)\n"
    "        self.assertEqual(clamp(2, 0, 3), 2)\n"
    "\n"
    "    # rule 3\n"
    "    def test_clamp_rejects_inverted_bounds(self):\n"
    "        with self.assertRaises(ValueError):\n"
    "            clamp(1, 3, 0)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
CALC_CASES = "ADD_CASES = [(2, 3, 5), (-1, 1, 0), (0, 0, 0)]\n"
CALC_PROMPT = (
    "Implement `calc.py`.\n\n"
    "1. `add(a, b)` returns the sum of two integers.\n"
    "2. `clamp(x, lo, hi)` returns x limited to the closed range [lo, hi].\n"
    "3. `clamp` raises ValueError when lo > hi.\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_pool(root: Path) -> Path:
    """Create ``root/calc-01-basics`` and return the pool directory."""
    task_dir = root / "calc-01-basics"
    _write(
        task_dir,
        "task.json",
        json.dumps(
            {
                "task_id": "calc-01-basics",
                "title": "Integer helpers",
                "category": "parse",
                "prompt": CALC_PROMPT,
                "test_command": "python3 -m unittest tests/test_calc.py",
                "impl_paths": ["calc.py"],
                "max_diff_lines": 40,
            }
        ),
    )
    _write(task_dir, "start/calc.py", CALC_START)
    _write(task_dir, "start/README.txt", "helpers\n")
    _write(task_dir, "tests/test_calc.py", CALC_TESTS)
    _write(task_dir, "tests/calc_cases.py", CALC_CASES)
    _write(task_dir, "reference/calc.py", CALC_REFERENCE)
    for name, src in CALC_MUTANTS.items():
        _write(task_dir, f"mutants/{name}/calc.py", src)
    (task_dir / "tests" / "__pycache__").mkdir()
    (task_dir / "tests" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    return root
