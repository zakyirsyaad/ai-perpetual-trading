import re
import subprocess
import sys
import unittest
from pathlib import Path

import forward_schema

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "supabase" / "schema.sql"
GENERATED = REPO / "supabase" / "long_forward_schema.sql"
SCRIPT = REPO / "src" / "forward_schema.py"


def base_sql() -> str:
    return BASE.read_text(encoding="utf-8")


class ForwardSchemaTests(unittest.TestCase):
    def test_checked_in_file_equals_the_generator_output(self):
        expected = forward_schema.schema_sql("long_forward", base_sql())
        self.assertEqual(GENERATED.read_text(encoding="utf-8"), expected)

    def test_no_public_reference_left(self):
        sql = forward_schema.schema_sql("long_forward", base_sql())
        self.assertNotIn("public.", sql)
        self.assertIsNone(re.search(r"\bpublic\b", sql, re.IGNORECASE))
        self.assertIn("create table if not exists long_forward.trades", sql)
        self.assertIn("alter table long_forward.open_position_state enable row level security;", sql)

    def test_every_qualified_name_is_rewritten(self):
        base = base_sql()
        sql = forward_schema.schema_sql("long_forward", base)
        self.assertEqual(sql.count("long_forward."), base.count("public."))

    def test_grants_and_create_schema_present(self):
        sql = forward_schema.schema_sql("long_forward", base_sql())
        lines = sql.splitlines()
        self.assertIn("create schema if not exists long_forward;", lines)
        self.assertLess(
            lines.index("create schema if not exists long_forward;"),
            lines.index("create table if not exists long_forward.trades ("),
        )
        for grant in (
            "grant usage on schema long_forward to service_role;",
            "grant all on all tables in schema long_forward to service_role;",
            "grant all on all sequences in schema long_forward to service_role;",
        ):
            self.assertIn(grant, lines)
            self.assertGreater(lines.index(grant), lines.index("create table if not exists long_forward.trades ("))

    def test_first_line_marks_the_file_as_generated(self):
        first = forward_schema.schema_sql("long_forward", base_sql()).splitlines()[0]
        self.assertTrue(first.startswith("--"))
        self.assertIn("src/forward_schema.py", first)
        self.assertIn("schema.sql", first)

    def test_bad_schema_names_refused(self):
        for name in (
            "public",
            "PUBLIC",
            "Long_Forward",
            "1abc",
            "long-forward",
            "long forward",
            "",
            "a;drop table x",
            "long_forward\n",
            'x"y',
            "pg_catalog",
            "information_schema",
            "a" * 64,
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    forward_schema.schema_sql(name, base_sql())

    def test_prefix_match_only_on_a_word_boundary(self):
        base = "select 1 from xpublic.t, public.t, a.public.t;"
        with self.assertRaises(ValueError):
            # a bare `public` token left behind is refused, not silently emitted
            forward_schema.schema_sql("lf", base)
        out = forward_schema.schema_sql("lf", "select * from xpublic.t, public.t;")
        self.assertIn("xpublic.t, lf.t;", out)

    def test_base_without_public_reference_refused(self):
        with self.assertRaises(ValueError):
            forward_schema.schema_sql("long_forward", "select 1;\n")

    def test_cli_prints_the_checked_in_file(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--schema", "long_forward"],
            capture_output=True, text=True, cwd="/", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, GENERATED.read_text(encoding="utf-8"))

    def test_cli_refuses_a_bad_name_with_one_error_line(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--schema", "public"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))

    def test_cli_reads_an_explicit_base(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.sql"
            path.write_text("create table public.t (id int);\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--schema", "lf", "--base", str(path)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create table lf.t (id int);", result.stdout)

    def test_cli_missing_base_is_one_error_line(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--schema", "lf", "--base", "/nonexistent/x.sql"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertTrue(result.stderr.startswith("error:"))


if __name__ == "__main__":
    unittest.main()
