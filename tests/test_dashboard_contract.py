"""The dashboard's TypeScript wire types must say what the Python wire models emit.

The dashboard is a separate program, so nothing at build time ties `bot-types.ts` to
`bot/telemetry/wire.py`. This test does: it reads the interfaces from the TypeScript
source and compares their keys with the pydantic models. A key added on one side and
forgotten on the other fails here instead of showing up as a blank panel in production.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from pydantic.alias_generators import to_camel

from bot.telemetry import wire

TYPES_FILE = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "lib" / "bot-types.ts"

PROP = re.compile(r"^\s*(\w+)(\?)?\s*:")
INLINE_OBJECT = re.compile(r"^\s*\w+\??\s*:\s*\{([^{}]*)\}\s*;?\s*$")

# TypeScript interface -> pydantic model.
INTERFACES = {
    "BlockEvent": wire.BlockEvent,
    "Decision": wire.Decision,
    "Quote": wire.Quote,
    "Fill": wire.FillData,
    "Position": wire.Position,
    "Totals": wire.Totals,
    "SleeveMeta": wire.SleeveMeta,
    "Meta": wire.Meta,
    "PricePoint": wire.PricePoint,
}
# (interface, property holding an inline object type) -> pydantic model.
NESTED = {
    ("BlockEvent", "resting"): wire.Resting,
    ("Decision", "probabilities"): wire.Probabilities,
    ("PricePoint", "fill"): wire.PricePointFill,
}


class Prop:
    def __init__(self, optional: bool) -> None:
        self.optional = optional
        self.nested: list[str] | None = None


def interface_lines(source: str, name: str) -> list[str]:
    start = re.search(rf"export interface {name} \{{\n", source)
    if start is None:
        raise AssertionError(f"interface {name} not found in {TYPES_FILE}")
    body: list[str] = []
    depth = 1
    for line in source[start.end():].splitlines():
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            return body
        body.append(line)
    raise AssertionError(f"interface {name} is not closed")


def properties(lines: list[str]) -> dict[str, Prop]:
    """Top-level properties of an interface body; a multi-line `{ ... }` type keeps its lines."""
    found: dict[str, Prop] = {}
    current: Prop | None = None
    depth = 0
    for line in lines:
        if depth == 0:
            match = PROP.match(line)
            if match:
                current = found[match.group(1)] = Prop(match.group(2) == "?")
        delta = line.count("{") - line.count("}")
        inline = INLINE_OBJECT.match(line) if depth == 0 else None
        if inline and current is not None:
            current.nested = [part.strip() for part in inline.group(1).split(";") if part.strip()]
        elif depth == 0 and delta > 0 and current is not None:
            current.nested = []
        elif depth > 0 and current is not None and current.nested is not None:
            current.nested.append(line)
        depth += delta
    return found


def wire_keys(model: type[wire._Wire]) -> set[str]:
    return {field.alias or to_camel(name) for name, field in model.model_fields.items()}


class DashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = TYPES_FILE.read_text()

    def props(self, interface: str) -> dict[str, Prop]:
        return properties(interface_lines(self.source, interface))

    def nested_props(self, interface: str, prop: str) -> dict[str, Prop]:
        nested = self.props(interface)[prop].nested
        assert nested is not None, f"{interface}.{prop} is not a multi-line inline object type"
        return properties(nested)

    def test_every_interface_has_exactly_the_keys_the_server_emits(self) -> None:
        for name, model in INTERFACES.items():
            with self.subTest(interface=name):
                self.assertEqual(set(self.props(name)), wire_keys(model), f"{name}: TypeScript vs {model.__name__}")

    def test_nested_objects_have_exactly_the_keys_the_server_emits(self) -> None:
        for (interface, prop), model in NESTED.items():
            with self.subTest(interface=interface, prop=prop):
                self.assertEqual(set(self.nested_props(interface, prop)), wire_keys(model), f"{interface}.{prop}")

    def test_a_key_the_server_may_omit_is_optional_in_typescript(self) -> None:
        for name, model in INTERFACES.items():
            with self.subTest(interface=name):
                props = self.props(name)
                required = {key for key in model._omit_when_none if not props[key].optional}
                self.assertEqual(required, set(), f"{name}: keys sent only sometimes must be `key?:`")

    def test_nested_keys_the_server_may_omit_are_optional_in_typescript(self) -> None:
        for (interface, prop), model in NESTED.items():
            with self.subTest(interface=interface, prop=prop):
                props = self.nested_props(interface, prop)
                required = {key for key in model._omit_when_none if not props[key].optional}
                self.assertEqual(required, set(), f"{interface}.{prop}: keys sent only sometimes must be `key?:`")

    def test_the_parser_tells_required_from_optional_keys(self) -> None:
        props = self.props("Quote")

        self.assertTrue(props["reduceOnly"].optional)
        self.assertFalse(props["side"].optional)


if __name__ == "__main__":
    unittest.main()
