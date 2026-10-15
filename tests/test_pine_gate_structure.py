"""Structural rules of the research package, checked on its source (AST)."""

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research.pine_gate import fileio, lock

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


# Imported lazily by the CLI for I/O only (reading the env, opening the Supabase
# store); neither shapes a computed result, and both change often on main.
_IO_ONLY_BOT_IMPORTS = frozenset({"bot/config.py", "bot/storage/supabase_trade_store.py"})


def _bot_sources_imported_by_research():
    """src-relative .py paths of every ``bot.*`` module any research module imports."""
    found = set()
    for path in sorted((SRC / "research").rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            for name in names:
                rel = Path(*name.split(".")).with_suffix(".py")
                if name.split(".")[0] == "bot" and (SRC / rel).is_file():
                    found.add(rel.as_posix())
    return found


class CodeFingerprintCoverageTests(unittest.TestCase):
    def test_fingerprint_covers_every_bot_module_the_research_imports(self):
        imported = _bot_sources_imported_by_research() - _IO_ONLY_BOT_IMPORTS
        self.assertIn("bot/strategy/indicators.py", imported)  # the walk finds something
        covered = {rel for rel, _sha in lock.code_files()}
        self.assertEqual(sorted(imported - covered), [])


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


BOT = SRC / "bot"
FORBIDDEN_IN_RESEARCH = ("bingx_client", "_signed_request", "X-BX-APIKEY", "listenKey")


def _imports(tree):
    """Every module a source names in an import statement or a dynamic import call
    (``importlib.import_module("x")``, ``import_module("x")``, ``__import__("x")``)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and _is_dynamic_import(node.func) and node.args:
            first = node.args[0]
            yield first.value if isinstance(first, ast.Constant) else "<dynamic>"


def _is_dynamic_import(func):
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _live_sources():
    """Every source outside the research package: the bot, main.py and the scripts."""
    return [p for p in sorted(SRC.rglob("*.py")) if "research" not in p.relative_to(SRC).parts]


class IsolationTests(unittest.TestCase):
    """The bot never imports research; research never touches private venue code (E8)."""

    def test_no_bot_module_or_main_imports_research(self):
        paths = _live_sources()
        self.assertIn(SRC / "main.py", paths)
        self.assertGreater(len([p for p in paths if BOT in p.parents]), 20)
        offenders = [f"{p.relative_to(SRC)}: {name}" for p in paths
                     for name in _imports(_tree(p))
                     if name in ("research", "<dynamic>") or name.startswith("research.")]
        self.assertEqual(offenders, [])

    def test_the_guard_sees_dynamic_imports(self):
        source = ("import importlib\n"
                  "importlib.import_module('research.pine_gate')\n"
                  "__import__('research')\n"
                  "from importlib import import_module\n"
                  "import_module(name)\n"
                  "from bot.strategy import pine_indicators\n")
        names = list(_imports(ast.parse(source)))
        for name in ("research.pine_gate", "research", "<dynamic>",
                     "bot.strategy.pine_indicators"):
            self.assertIn(name, names)

    def test_no_live_module_imports_pine_indicators(self):
        target = "bot.strategy.pine_indicators"
        offenders = []
        for p in _live_sources():
            if p == BOT / "strategy" / "pine_indicators.py":
                continue
            tree = _tree(p)
            names = list(_imports(tree))
            relative = [f"{node.module or ''}.{alias.name}" for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom) and node.level >= 1
                        for alias in node.names]
            if target in names or any("pine_indicators" in name for name in relative):
                offenders.append(str(p.relative_to(SRC)))
        self.assertEqual(offenders, [])

    def test_research_never_references_private_venue_code(self):
        paths = sorted((SRC / "research").rglob("*.py"))
        self.assertGreater(len(paths), 20)
        offenders = [f"{p.relative_to(SRC)}: {word}" for p in paths
                     for word in FORBIDDEN_IN_RESEARCH
                     if word in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])


class TestLayoutTests(unittest.TestCase):
    def test_the_main_guard_is_the_last_statement_of_every_pine_test_module(self):
        late = []
        for path in sorted(Path(__file__).resolve().parent.glob("test_pine*.py")):
            body = _tree(path).body
            guards = [i for i, node in enumerate(body) if isinstance(node, ast.If)
                      and "__main__" in ast.dump(node.test)]
            if guards and guards[-1] != len(body) - 1:
                late.append(path.name)
        self.assertEqual(late, [])

    def test_vst_agreement_has_no_dead_overshoot_parameter(self):
        import inspect

        from research.pine_gate.checks import vst_agreement

        self.assertNotIn("overshoot_bps", inspect.signature(vst_agreement).parameters)


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
