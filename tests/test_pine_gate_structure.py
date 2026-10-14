"""Structural rules of the research package, checked on its source (AST)."""

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research.pine_gate import fileio

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "research" / "pine_gate"


def _modules():
    return sorted(PACKAGE.glob("*.py"))


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class PrivateImportTests(unittest.TestCase):
    def test_no_module_imports_a_private_name_of_another(self):
        offenders = []
        for path in _modules():
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom) and node.level >= 1:
                    for alias in node.names:
                        if alias.name.startswith("_") and not alias.name.startswith("__"):
                            offenders.append(f"{path.name}: {node.module}.{alias.name}")
        self.assertEqual(offenders, [])


class ExclusiveWriterTests(unittest.TestCase):
    def test_only_fileio_hard_links_files(self):
        callers = []
        for path in _modules():
            for node in ast.walk(_tree(path)):
                if (isinstance(node, ast.Attribute) and node.attr == "link"
                        and isinstance(node.value, ast.Name) and node.value.id == "os"):
                    callers.append(path.name)
        self.assertEqual(sorted(set(callers)), ["fileio.py"])

    def test_write_exclusive_never_replaces_and_names_the_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "sel.json"
            fileio.write_json_exclusive(path, {"a": 1}, "selection")
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertTrue(path.read_text().endswith("}\n"))
            with self.assertRaises(FileExistsError) as ctx:
                fileio.write_json_exclusive(path, {"a": 2}, "selection")
            self.assertIn("selection file already exists", str(ctx.exception))
            self.assertIn(str(path), str(ctx.exception))
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["sel.json"])

    def test_a_concurrent_writer_wins_the_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pine_gate_holdout_20261014.json"

            def late_link(src, dst):
                Path(dst).write_text("{}")
                raise FileExistsError(dst)

            with mock.patch.object(fileio.os, "link", side_effect=late_link):
                with self.assertRaises(FileExistsError) as ctx:
                    fileio.write_json_exclusive(path, {"a": 1}, "holdout")
            self.assertIn("holdout file already exists", str(ctx.exception))
            self.assertEqual(path.read_text(), "{}")
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_text_is_written_exactly_and_nan_is_refused_before_any_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            fileio.write_exclusive(path, '{"x":1}')
            self.assertEqual(path.read_bytes(), b'{"x":1}')
            with self.assertRaises(ValueError):
                fileio.write_json_exclusive(Path(tmp) / "nan.json", {"x": float("nan")})
            self.assertFalse((Path(tmp) / "nan.json").exists())


class LockModuleTests(unittest.TestCase):
    def test_lock_names_are_public_and_shared(self):
        from research.pine_gate import holdout, lock, pipeline

        for name in ("HoldoutLockError", "Segments", "load_selection_checked",
                     "latencies_sha256", "equal_ranges"):
            self.assertIs(getattr(pipeline, name), getattr(lock, name), name)
        self.assertIs(holdout.HoldoutLockError, lock.HoldoutLockError)
        self.assertTrue(callable(lock.code_sha256))
        self.assertTrue(callable(lock.run_settings))


if __name__ == "__main__":
    unittest.main()
