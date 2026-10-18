"""Structural rules of the mode backtest package, checked on its source.

Every rule runs over each ``*.py`` file of ``src/research/mode_backtest``, so a module
added by a later task is covered without touching this file. Each detector is also
run on small synthetic sources, so a rule cannot pass just because it looks at nothing.

The code fingerprint of the lock is the package, the rule core and every module they
import: ``lock.import_closure``, an AST walk of every import statement followed from module
to module through ``bot`` and ``research``. It is held against two walks written apart from
it: the standard library's ``modulefinder``, which reads the compiled imports (those inside
functions too), and the modules a fresh Python process has loaded once it has imported every
module of the package.

One rule looks the other way: no live module (every source under ``src`` outside
``research/`` and the rule core ``bot/strategy/modes/``) imports the rule core.
"""

import ast
import json
import modulefinder
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import List, Set

from research.mode_backtest import lock
from research.mode_backtest.lock import imported_names, module_name, project_modules

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


# Run by a fresh interpreter: import every module of the package, then print the source files
# under ``src`` (the first argument) of every module Python has loaded.
LOADED_BY_THE_PACKAGE = """
import importlib, json, pkgutil, sys
from pathlib import Path
import research.mode_backtest as package
for info in pkgutil.iter_modules(package.__path__):
    importlib.import_module(f"research.mode_backtest.{info.name}")
src = Path(sys.argv[1]).resolve()
files = {
    Path(module.__file__).resolve().relative_to(src).as_posix()
    for module in list(sys.modules.values())
    if getattr(module, "__file__", None) and Path(module.__file__).resolve().is_relative_to(src)
}
print(json.dumps(sorted(files)))
"""


def relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


RULE_CORE = "bot.strategy.modes"
RULE_CORE_DIR = SRC / "bot" / "strategy" / "modes"
DYNAMIC = "<dynamic>"  # a dynamic import whose module name is not a constant


# The one live module that may import the rule core: the LONG forward bot's regime gate, which
# reads the backtest's own hourly selector (``bot.strategy.modes.regime``).
RULE_CORE_USERS = frozenset({SRC / "bot" / "core" / "regime_gate.py"})


def live_sources() -> List[Path]:
    """Every source under ``src`` outside ``research/`` and the rule core: the bot, ``main.py``
    and the scripts."""
    return [
        path
        for path in sorted(SRC.rglob("*.py"))
        if "research" not in path.relative_to(SRC).parts and RULE_CORE_DIR not in path.parents
    ]


def is_dynamic_import(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def dynamic_imports(tree: ast.AST) -> Set[str]:
    """The modules that ``importlib.import_module("x")``, ``import_module("x")`` and
    ``__import__("x")`` name, and ``DYNAMIC`` for a name that is not a constant."""
    return {
        node.args[0].value if is_text_constant(node.args[0]) else DYNAMIC
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and is_dynamic_import(node.func) and node.args
    }


def rule_core_imports(tree: ast.AST, module: str, is_package: bool) -> List[str]:
    """The names under ``bot.strategy.modes`` that ``module`` may load, relative imports resolved
    (``imported_names``) and dynamic ones included, plus ``DYNAMIC`` when a dynamic import's name
    cannot be read."""
    names = imported_names(tree, module, is_package) | dynamic_imports(tree)
    return sorted(
        name for name in names
        if name in (RULE_CORE, DYNAMIC) or name.startswith(f"{RULE_CORE}.")
    )


def rule_core_imports_of(path: Path) -> List[str]:
    tree = parse(path.read_text(encoding="utf-8"))
    return rule_core_imports(tree, module_name(path), path.name == "__init__.py")


def rule_core_offenders(trees: "dict[Path, ast.AST]") -> List[str]:
    """``path: name`` for each rule core import of a live module that is not in ``RULE_CORE_USERS``."""
    return [
        f"{relative(path)}: {name}"
        for path, tree in trees.items()
        if path not in RULE_CORE_USERS
        for name in rule_core_imports(tree, module_name(path), path.name == "__init__.py")
    ]


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


# Imported inside functions the package never calls, only to read the live settings and
# Supabase (``research.pine_gate.cli_common.make_settings``, ``research.pine_gate.calibrate``).
# Written here and in the lock, so a module added to the lock's list by mistake cannot drop out
# of the fingerprint and of this check at once.
IO_ONLY = frozenset({"bot.config", "bot.storage.supabase_trade_store"})

# The packages around an excluded module: ``modulefinder`` opens them on its way to it, Python
# never runs that import, and either side may or may not hold them.
AROUND_IO_ONLY = frozenset(
    "/".join(name.split(".")[:k]) + "/__init__.py"
    for name in IO_ONLY for k in range(1, name.count(".") + 1)
)


def found_by_modulefinder(paths: List[Path]) -> Set[str]:
    """The project files the standard library's ``modulefinder`` reaches from the modules at
    ``paths``: a second walk, apart from the lock's, over the compiled code of every import
    statement, those inside functions and ``TYPE_CHECKING`` blocks included."""
    finder = modulefinder.ModuleFinder(path=[str(SRC)], excludes=sorted(IO_ONLY))
    for path in paths:
        finder.import_hook(module_name(path))
    files = {Path(module.__file__).resolve() for module in finder.modules.values()
             if module.__file__}
    return {relative(path) for path in files if path.is_relative_to(SRC)}


class CodeFingerprintTests(unittest.TestCase):
    def imported_from_outside(self) -> Set[str]:
        """The files of the modules the package imports that are not its own."""
        own = {relative(path) for path in modules()}
        return found_by_modulefinder(modules()) - own - AROUND_IO_ONLY

    def covered(self) -> Set[str]:
        return {rel for rel, _sha in lock.code_files()}

    def test_the_lock_leaves_out_only_the_io_only_imports_named_here(self):
        self.assertEqual(lock.IO_ONLY_IMPORTS, IO_ONLY)

    def test_fingerprint_covers_every_bot_and_pine_gate_module_the_package_imports(self):
        imported = self.imported_from_outside()

        for followed in (
            "bot/strategy/modes/near_mean.py",  # simulate -> near_mean
            "bot/core/entry_window.py",  # frames, simulate, position_rules -> entry_window
            "research/pine_gate/features.py",  # data -> features
            "bot/arena/setup_profile.py",  # features -> setup_profile
            "bot/strategy/indicators.py",  # features, near_mean -> indicators
            "bot/strategy/pine_indicators.py",  # frames, features -> pine_indicators
            "research/pine_gate/cli_common.py",  # cli, data, step0 -> cli_common
            "bot/arena/__init__.py",  # runs before setup_profile ...
            "bot/arena/factors.py",  # ... and imports the arena modules
        ):
            self.assertIn(followed, imported)
        self.assertEqual(sorted(imported - self.covered()), [])

    def test_the_fingerprint_is_what_an_independent_walk_reaches(self):
        starts = [*modules(), *sorted(RULE_CORE_DIR.glob("*.py"))]
        reached = found_by_modulefinder(starts) - AROUND_IO_ONLY
        covered = self.covered() - AROUND_IO_ONLY

        self.assertIn("bot/storage/__init__.py", AROUND_IO_ONLY)
        self.assertEqual(sorted(covered - reached), [])
        self.assertEqual(sorted(reached - covered), [])

    def test_python_loads_no_project_module_the_fingerprint_misses(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        done = subprocess.run(
            [sys.executable, "-c", LOADED_BY_THE_PACKAGE, str(SRC)],
            cwd=SRC.parent, env=env, capture_output=True, text=True, timeout=120,
        )

        self.assertEqual(done.returncode, 0, done.stderr)
        loaded = set(json.loads(done.stdout))
        self.assertIn("research/mode_backtest/stages.py", loaded)
        self.assertIn("bot/strategy/pine_indicators.py", loaded)
        self.assertEqual(sorted(loaded - self.covered()), [])

    def test_the_walk_resolves_every_form_of_import(self):
        source = (
            "import os\n"
            "import bot.strategy.indicators as live\n"
            "from bot.strategy import pine_indicators\n"
            "from . import data\n"
            "from .frames import build_pair_frames\n"
            "from ..pine_gate import bars\n"
            "def later():\n"
            "    from bot.core.entry_window import parse_weekly_window\n"
            "if TYPE_CHECKING:\n"
            "    from .step0 import Step0Row\n"
        )

        names = imported_names(parse(source), "research.mode_backtest.stages", is_package=False)

        self.assertEqual(project_modules(names), {
            "bot", "bot.strategy", "bot.strategy.indicators", "bot.strategy.pine_indicators",
            "bot.core", "bot.core.entry_window",
            "research", "research.mode_backtest", "research.mode_backtest.data",
            "research.mode_backtest.frames", "research.mode_backtest.step0",
            "research.pine_gate", "research.pine_gate.bars",
        })

    def test_relative_imports_of_a_package_start_from_the_package_itself(self):
        names = imported_names(parse("from . import data\nfrom .. import pine_gate\n"),
                               "research.mode_backtest", is_package=True)

        self.assertEqual(names, {"research.mode_backtest", "research.mode_backtest.data",
                                 "research", "research.pine_gate"})

    def test_the_walk_drops_io_only_imports_and_names_that_are_no_project_module(self):
        names = {"bot.config", "bot.storage.supabase_trade_store", "os", "yaml",
                 "research.pine_gate.bars.Bars", "bot.no_such_module"}

        self.assertEqual(project_modules(names), set())


class SharedCheckTests(unittest.TestCase):
    """The number and side checks have one copy, ``bot.strategy.modes.checks``: copies drift."""

    NAMES = ("is_whole", "is_finite", "is_positive_finite", "require_positive",
             "require_not_negative", "check_side")

    def test_no_module_writes_its_own_number_or_side_check(self):
        paths = [*modules(), *sorted(RULE_CORE_DIR.glob("*.py"))]
        self.assertIn(RULE_CORE_DIR / "checks.py", paths)

        defined = [
            f"{relative(path)}: {node.name}"
            for path in paths if path.name != "checks.py"
            for node in ast.walk(parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name.lstrip("_") in self.NAMES
        ]

        self.assertEqual(defined, [])


class RuleCoreIsolationTests(unittest.TestCase):
    """Only ``research/``, the rule core itself and ``bot/core/regime_gate.py`` import
    ``bot.strategy.modes``; wiring a mode into the live bot is a later phase with its own spec."""

    def test_no_live_module_imports_the_rule_core_but_the_regime_gate(self):
        paths = live_sources()
        self.assertIn(SRC / "main.py", paths)
        self.assertGreater(len([p for p in paths if SRC / "bot" in p.parents]), 20)
        self.assertIn(SRC / "bot" / "strategy" / "__init__.py", paths)
        # the guard sees the core in a real import (research's), and the scan leaves out
        # research and the core's own modules
        stages = PACKAGE / "stages.py"
        self.assertIn("bot.strategy.modes.config", rule_core_imports_of(stages))
        core = sorted(RULE_CORE_DIR.glob("*.py"))
        self.assertIn(RULE_CORE_DIR / "pullback.py", core)
        self.assertEqual([relative(p) for p in [stages, *core] if p in paths], [])

        offenders = rule_core_offenders(
            {path: parse(path.read_text(encoding="utf-8")) for path in paths}
        )

        self.assertEqual(offenders, [])
        # the one allowed user really imports the core, so the exception is not a dead entry
        (regime_gate,) = RULE_CORE_USERS
        self.assertIn("bot.strategy.modes.regime", rule_core_imports_of(regime_gate))

    def test_a_second_live_module_importing_the_rule_core_is_refused(self):
        regime_gate = next(iter(RULE_CORE_USERS))
        other = regime_gate.with_name("other_gate.py")
        tree = parse("from bot.strategy.modes.regime import pilih_mode\n")

        offenders = rule_core_offenders({regime_gate: tree, other: tree})

        self.assertEqual(
            offenders,
            [
                "bot/core/other_gate.py: bot.strategy.modes.regime",
                "bot/core/other_gate.py: bot.strategy.modes.regime.pilih_mode",
            ],
        )

    def test_the_rule_core_guard_sees_every_form_of_import(self):
        sources = (
            "import bot.strategy.modes\n",
            "import bot.strategy.modes.config as modes_config\n",
            "from bot.strategy import modes\n",
            "from bot.strategy.modes.pullback import PullbackTracker\n",
            "from . import modes\n",
            "from .modes import regime\n",
            "from ..strategy.modes.near_mean import near_mean_short\n",
            "def later():\n    from bot.strategy.modes import position_rules\n",
            "import importlib\nimportlib.import_module('bot.strategy.modes.config')\n",
            "__import__('bot.strategy.modes')\n",
            "from importlib import import_module\nimport_module(name)\n",
        )
        for source in sources:
            with self.subTest(source=source):
                found = rule_core_imports(parse(source), "bot.strategy.engine", is_package=False)
                self.assertNotEqual(found, [])

    def test_the_rule_core_guard_lets_other_modules_through(self):
        source = (
            "import modes\n"
            "import bot.strategy.indicators\n"
            "from bot import strategy\n"
            "from bot.strategy import pine_indicators\n"
            "from .modes_extra import helper\n"
            "from . import indicators\n"
            "import importlib\n"
            "importlib.import_module('bot.strategy.indicators')\n"
        )

        self.assertEqual(rule_core_imports(parse(source), "bot.strategy.engine", False), [])


if __name__ == "__main__":
    unittest.main()
