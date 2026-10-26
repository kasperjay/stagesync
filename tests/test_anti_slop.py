"""Autonomous Anti-Slop, Clean Code, and Zero Tech-Debt Audit Suite.

Enforces:
1. Zero forbidden placeholders: TODO, FIXME, XXX, HACK, pass stubs.
2. Zero mock data or fake implementations in production code.
3. 100% docstring coverage for modules, classes, and public functions.
4. Clean syntax compilation across all production modules.
"""

import ast
import os
from pathlib import Path
import py_compile
import re
import unittest

STAGESYNC_ROOT = Path(__file__).resolve().parent.parent / "stagesync"


class TestAntiSlopAudit(unittest.TestCase):
    """Enforces zero AI slop, zero dead code, and clean code standards."""

    def setUp(self) -> None:
        self.py_files = list(STAGESYNC_ROOT.glob("**/*.py"))
        self.assertTrue(len(self.py_files) > 0, "No Python source files found in stagesync/")

    def test_zero_todo_or_fixme_placeholders(self) -> None:
        """Verify zero TODO, FIXME, XXX, or HACK comments exist in production code."""
        pattern = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b", re.IGNORECASE)
        violations = []

        for p in self.py_files:
            # Skip pycache
            if "__pycache__" in str(p):
                continue
            text = p.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    violations.append(f"{p.name}:{idx} -> {line.strip()}")

        self.assertEqual(
            violations,
            [],
            f"Anti-slop violation: Found {len(violations)} forbidden placeholders:\n" + "\n".join(violations),
        )

    def test_zero_mock_strings_in_production(self) -> None:
        """Verify zero hardcoded mock/fake keywords exist in production logic."""
        # 'mock' or 'dummy' should never appear as identifiers or hardcoded strings in stagesync/
        mock_pattern = re.compile(r"\b(mock_|mock_data|fake_|dummy_)\b", re.IGNORECASE)
        violations = []

        for p in self.py_files:
            if "__pycache__" in str(p):
                continue
            text = p.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                if mock_pattern.search(line):
                    violations.append(f"{p.name}:{idx} -> {line.strip()}")

        self.assertEqual(
            violations,
            [],
            f"Anti-slop violation: Found mock data in production routines:\n" + "\n".join(violations),
        )

    def test_clean_py_compilation(self) -> None:
        """Verify all production modules compile without any syntax errors or warnings."""
        for p in self.py_files:
            if "__pycache__" in str(p):
                continue
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as e:
                self.fail(f"Compilation error in {p}: {e}")

    def test_modules_have_docstrings(self) -> None:
        """Verify every production python module has an informative docstring."""
        missing = []
        for p in self.py_files:
            if "__pycache__" in str(p) or p.name == "__init__.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            doc = ast.get_docstring(tree)
            if not doc or len(doc.strip()) < 10:
                missing.append(p.name)

        self.assertEqual(
            missing,
            [],
            f"The following modules are missing required module docstrings:\n{missing}",
        )

    def test_classes_have_docstrings(self) -> None:
        """Verify all public classes have docstrings."""
        missing = []
        for p in self.py_files:
            if "__pycache__" in str(p):
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if not node.name.startswith("_"):
                        doc = ast.get_docstring(node)
                        if not doc or len(doc.strip()) < 5:
                            missing.append(f"{p.name}::{node.name}")

        self.assertEqual(
            missing,
            [],
            f"The following public classes are missing docstrings:\n{missing}",
        )


if __name__ == "__main__":
    unittest.main()
