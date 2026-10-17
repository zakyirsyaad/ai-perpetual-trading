"""Structural rules of the mode backtest package, checked on its source.

Every rule runs over each ``*.py`` file of ``src/research/mode_backtest``, so a module
added by a later task is covered without touching this file. Each detector is also
run on small synthetic sources, so a rule cannot pass just because it looks at nothing.
"""

import ast
import re
import unittest
from pathlib import Path
from typing import List, Set

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "research" / "mode_backtest"

# Code of this project: a private name of any of it is off limits.
PROJECT_ROOTS = ("research", "bot")

# Matched on the raw text, so comments and docstrings must not contain them either.
FORBIDDEN_WORDS = (
    "httpx",
    "socket",
    "urllib",
    "requests",
    "bingx_client",
    "_signed_request",
    "X-BX-APIKEY",
    "listenKey",
)

WRITE_METHODS = ("write_text", "write_bytes")
OPENERS = ("open", "fdopen", "GzipFile")
MODE = re.compile(r"[rwxabtU+]+")


def modules() -> List[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def parse(source: str) -> ast.AST:
    return ast.parse(source)


def is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def targets_project_code(node: ast.ImportFrom) -> bool:
    return node.level >= 1 or (node.module or "").split(".")[0] in PROJECT_ROOTS


def private_imports(tree: ast.AST) -> List[str]:
    """Imports of a private name, or of a private module, of project code."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and targets_project_code(node):
            names = (node.module or "").split(".") + [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [
                part
                for alias in node.names
                if alias.name.split(".")[0] in PROJECT_ROOTS
                for part in alias.name.split(".")
            ]
        else:
            continue
        found += [f"line {node.lineno}: {name}" for name in names if name and is_private(name)]
    return found


def imported_project_names(tree: ast.AST) -> Set[str]:
    """Local names that an import statement binds to project code."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PROJECT_ROOTS:
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and targets_project_code(node):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def private_attribute_reaches(tree: ast.AST) -> List[str]:
    """``imported_name._private`` or ``imported_name.sub._private``: a private name reached
    through an import that the import-from check could not see."""
    imported = imported_project_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and is_private(node.attr):
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in imported:
                found.append(f"line {node.lineno}: {base.id}...{node.attr}")
    return found


def forbidden_words(text: str) -> List[str]:
    return [word for word in FORBIDDEN_WORDS if word in text]


def callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def is_writing_mode(text: str) -> bool:
    return MODE.fullmatch(text) is not None and any(c in text for c in "wax+")


def is_text_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def file_writes(tree: ast.AST) -> List[str]:
    """``write_text``, ``write_bytes`` and ``open``-style calls in a mode that can write."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in WRITE_METHODS:
            found.append(f"line {node.lineno}: {node.attr}")
        elif isinstance(node, ast.Call) and callee_name(node.func) in OPENERS:
            # The mode is the second argument of open() and the first of Path.open().
            for arg in node.args[:2]:
                if is_text_constant(arg) and is_writing_mode(arg.value):
                    call = f"{callee_name(node.func)}(..., {arg.value!r})"
                    found.append(f"line {node.lineno}: {call}")
            for keyword in node.keywords:
                if keyword.arg == "mode" and not (
                    is_text_constant(keyword.value) and not is_writing_mode(keyword.value.value)
                ):
                    found.append(f"line {node.lineno}: {callee_name(node.func)}(mode=...)")
    return found


class PackageLayoutTests(unittest.TestCase):
    def test_the_rules_below_look_at_real_modules(self):
        names = [p.name for p in modules()]

        self.assertIn("__init__.py", names)
        self.assertIn("data.py", names)


class PrivateImportTests(unittest.TestCase):
    def test_no_module_imports_a_private_name_of_another(self):
        offenders = [
            f"{path.name}: {found}"
            for path in modules()
            for found in private_imports(parse(path.read_text(encoding="utf-8")))
        ]

        self.assertEqual(offenders, [])

    def test_no_module_reaches_a_private_name_through_an_imported_module(self):
        offenders = [
            f"{path.name}: {found}"
            for path in modules()
            for found in private_attribute_reaches(parse(path.read_text(encoding="utf-8")))
        ]

        self.assertEqual(offenders, [])

    def test_the_import_guard_sees_every_form_of_a_private_import(self):
        source = (
            "from . import _helper\n"
            "from .data import _load\n"
            "from ._hidden import shown\n"
            "from research.pine_gate.bars import _empty\n"
            "from research.mode_backtest.data import _WARMUP_INTERVALS as warmups\n"
            "from bot.strategy.pine_indicators import _rolling\n"
            "import research.pine_gate._internal\n"
        )

        self.assertEqual(len(private_imports(parse(source))), 7)

    def test_the_import_guard_lets_public_names_and_other_packages_through(self):
        source = (
            "from . import data\n"
            "from .data import load_pair\n"
            "from research.pine_gate.bars import Bars, __doc__\n"
            "from bot.strategy.modes.config import load_modes_config\n"
            "from os import _exit\n"
            "import research.pine_gate.bars\n"
            "import os._private\n"
        )

        self.assertEqual(private_imports(parse(source)), [])

    def test_the_attribute_guard_sees_a_private_name_behind_an_import(self):
        source = (
            "from research.pine_gate import bars\n"
            "import research.pine_gate.lock as pine_lock\n"
            "import research.pine_gate.fileio\n"
            "from . import data\n"
            "bars._empty(1)\n"
            "pine_lock._SRC_ROOT\n"
            "research.pine_gate.fileio._tmp\n"
            "data.sub._private\n"
        )

        self.assertEqual(len(private_attribute_reaches(parse(source))), 4)

    def test_the_attribute_guard_lets_public_names_and_own_privates_through(self):
        source = (
            "from research.pine_gate import bars\n"
            "from typing import NamedTuple\n"
            "class Point(NamedTuple):\n"
            "    x: int\n"
            "    def moved(self):\n"
            "        return self._replace(x=1)\n"
            "bars.load_days\n"
            "bars.Bars.__len__\n"
            "point = Point(1)\n"
            "point._asdict()\n"
        )

        self.assertEqual(private_attribute_reaches(parse(source)), [])


class NetworkAndVenueTests(unittest.TestCase):
    def test_no_network_or_venue_code(self):
        offenders = [
            f"{path.name}: {word}"
            for path in modules()
            for word in forbidden_words(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(offenders, [])

    def test_the_word_guard_sees_every_word_in_code_comments_and_docstrings(self):
        for word in FORBIDDEN_WORDS:
            for source in (f"import {word}", f"# {word}", f'"""{word}"""', f"x = '{word}'"):
                with self.subTest(source=source):
                    self.assertEqual(forbidden_words(source), [word])
        self.assertEqual(forbidden_words("from research.pine_gate.bars import Bars"), [])


class FileWriteTests(unittest.TestCase):
    def test_files_are_written_only_through_fileio(self):
        offenders = [
            f"{path.name}: {found}"
            for path in modules()
            for found in file_writes(parse(path.read_text(encoding="utf-8")))
        ]

        self.assertEqual(offenders, [])

    def test_the_write_guard_sees_every_way_to_write_a_file(self):
        writes = [
            "path.write_text('x')",
            "path.write_bytes(b'x')",
            "open('f', 'w')",
            "open('f', 'a')",
            "open('f', 'x')",
            "open('f', 'wb')",
            "open('f', 'r+')",
            "open('f', mode='w')",
            "open('f', mode=chosen)",
            "path.open('w')",
            "gzip.open(path, 'wt')",
            "gzip.GzipFile(path, 'wb')",
            "os.fdopen(fd, 'wb')",
        ]
        for source in writes:
            with self.subTest(source=source):
                self.assertEqual(len(file_writes(parse(source))), 1)

    def test_the_write_guard_lets_reading_through(self):
        reads = [
            "open('f')",
            "open('f', 'r')",
            "open('f', 'rb')",
            "open('f', mode='rt')",
            "path.open('rb')",
            "gzip.open(path, 'rt', encoding='utf-8')",
            "path.read_text()",
            "path.read_bytes()",
            "webbrowser.open('page')",
            "from research.pine_gate.fileio import write_exclusive\nwrite_exclusive(path, data)",
        ]
        for source in reads:
            with self.subTest(source=source):
                self.assertEqual(file_writes(parse(source)), [])


if __name__ == "__main__":
    unittest.main()
