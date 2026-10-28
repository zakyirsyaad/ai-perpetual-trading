"""`forward_schema.py --only TABLE`: one table's statements, for a schema that already exists.

`long_forward` was created before `shadow_entries` joined `supabase/schema.sql`, so re-running the whole
generated schema is not the way to add that table; `--only` prints just its statements, derived from the
same base so the two cannot drift.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import forward_schema

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "supabase" / "schema.sql"
GENERATED = REPO / "supabase" / "long_forward_shadow_entries.sql"
SCRIPT = REPO / "src" / "forward_schema.py"

GRANTS = [
    "grant usage on schema long_forward to service_role;",
    "grant all on all tables in schema long_forward to service_role;",
    "grant all on all sequences in schema long_forward to service_role;",
]
OTHER_TABLES = (
    "trades", "ai_decisions", "arena_diagnostics", "ai_exit_verdicts", "ai_call_metrics", "open_position_state",
)


def base_sql() -> str:
    return BASE.read_text(encoding="utf-8")


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd="/", check=False
    )


class ShadowEntriesOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = forward_schema.table_sql("long_forward", "shadow_entries", base_sql())

    def test_first_line_marks_the_file_as_generated(self) -> None:
        first = self.sql.splitlines()[0]
        self.assertTrue(first.startswith("--"))
        self.assertIn("src/forward_schema.py", first)
        self.assertIn("schema.sql", first)
        self.assertIn("shadow_entries", first)

    def test_holds_the_table_its_index_and_row_level_security_in_that_order(self) -> None:
        positions = [
            self.sql.index(text)
            for text in (
                "create table if not exists long_forward.shadow_entries (",
                "create index if not exists shadow_entries_exchange_evaluated_decided_idx "
                "on long_forward.shadow_entries (exchange, evaluated_at, decided_at);",
                "alter table long_forward.shadow_entries enable row level security;",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        for column in ("decided_at timestamptz not null", "bucket_key text not null", "gross_bps double precision"):
            self.assertIn(column, self.sql)

    def test_ends_with_the_schemas_grants_and_nothing_after_them(self) -> None:
        self.assertTrue(self.sql.endswith("\n"))
        self.assertEqual(self.sql.rstrip("\n").splitlines()[-3:], GRANTS)

    def test_holds_no_other_table_and_no_public_reference(self) -> None:
        for name in OTHER_TABLES:
            self.assertNotIn(name, self.sql)
        self.assertIsNone(re.search(r"\bpublic\b", self.sql, re.IGNORECASE))
        self.assertEqual(self.sql.count("create table"), 1)

    def test_the_comment_above_the_table_survives_whole(self) -> None:
        # the base comment holds a semicolon: splitting on it would cut the comment off the table
        self.assertIn("Observability only; nothing here places an order.", self.sql)
        self.assertLess(self.sql.index("-- Hypothetical entries"), self.sql.index("create table"))

    def test_every_create_is_safe_to_run_again(self) -> None:
        creates = [line for line in self.sql.splitlines() if re.match(r"create (unique )?(table|index)", line)]
        self.assertGreaterEqual(len(creates), 2)
        for line in creates:
            self.assertIn("if not exists", line)


class CheckedInShadowEntriesFileTests(unittest.TestCase):
    def test_checked_in_file_equals_the_generator_output(self) -> None:
        expected = forward_schema.table_sql("long_forward", "shadow_entries", base_sql())
        self.assertEqual(GENERATED.read_text(encoding="utf-8"), expected)

    def test_cli_prints_the_checked_in_file(self) -> None:
        result = run_cli("--schema", "long_forward", "--only", "shadow_entries")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, GENERATED.read_text(encoding="utf-8"))
        self.assertEqual(result.stderr, "")


class EveryTableTests(unittest.TestCase):
    def test_every_table_of_the_base_can_be_extracted_on_its_own(self) -> None:
        base = base_sql()
        tables = re.findall(r"create table if not exists public\.(\w+)", base)
        self.assertGreaterEqual(len(tables), 7)
        for table in tables:
            with self.subTest(table=table):
                sql = forward_schema.table_sql("lf", table, base)
                self.assertEqual(sql.count("create table"), 1)
                self.assertIn(f"create table if not exists lf.{table} (", sql)
                self.assertIsNone(re.search(r"\bpublic\b", sql, re.IGNORECASE))
                self.assertEqual(sql.count(f"alter table lf.{table} "), base.count(f"alter table public.{table} "))
                self.assertEqual(sql.count(f" on lf.{table} "), base.count(f" on public.{table} "))
                self.assertTrue(sql.rstrip("\n").endswith("grant all on all sequences in schema lf to service_role;"))

    def test_the_full_schema_generator_is_unchanged_by_the_refactor(self) -> None:
        full = forward_schema.schema_sql("long_forward", base_sql())
        self.assertTrue(full.endswith("\n".join(GRANTS) + "\n"))


class StatementOwnershipTests(unittest.TestCase):
    def only(self, table: str, base: str) -> str:
        return forward_schema.table_sql("lf", table, base)

    def test_semicolons_in_comments_and_strings_do_not_split_a_statement(self) -> None:
        base = (
            "-- first; second public.other; third\n"
            "create table if not exists public.t (id int default 0, note text default 'a;b');\n"
            "create table if not exists public.other (id int);\n"
        )
        out = self.only("t", base)
        self.assertIn("note text default 'a;b');", out)
        self.assertIn("-- first; second", out)
        self.assertNotIn("create table if not exists lf.other", out)

    def test_apostrophes_inside_comments_do_not_open_a_string(self) -> None:
        base = "-- the bot's gate; it's fine, 'okx' stays\ncreate table if not exists public.t (id int);\n"
        out = self.only("t", base)
        self.assertIn("create table if not exists lf.t (id int);", out)
        self.assertIn("-- the bot's gate; it's fine, 'okx' stays", out)

    def test_double_quoted_identifiers_are_skipped_like_strings(self) -> None:
        base = 'create table if not exists public.t ("we;ird--name" int);\n'
        self.assertIn('"we;ird--name" int);', self.only("t", base))

    def test_a_statement_belongs_to_the_first_table_it_names(self) -> None:
        base = (
            "create table if not exists public.parent (id int primary key);\n"
            "create table if not exists public.child (id int, parent_id int references public.parent (id));\n"
            "create index if not exists child_parent_idx on public.child (parent_id);\n"
            "alter table public.parent enable row level security;\n"
        )
        child = self.only("child", base)
        self.assertIn("references lf.parent (id)", child)
        self.assertIn("create index if not exists child_parent_idx on lf.child (parent_id);", child)
        self.assertNotIn("create table if not exists lf.parent", child)
        self.assertNotIn("alter table lf.parent", child)
        parent = self.only("parent", base)
        self.assertIn("alter table lf.parent enable row level security;", parent)
        self.assertNotIn("lf.child", parent)

    def test_a_comment_naming_another_table_does_not_move_the_statement(self) -> None:
        base = (
            "-- see public.other for the history\n"
            "create table if not exists public.t (id int);\n"
            "create table if not exists public.other (id int);\n"
        )
        out = self.only("t", base)
        self.assertIn("-- see lf.other for the history\ncreate table if not exists lf.t (id int);", out)
        self.assertNotIn("create table if not exists lf.other", out)

    def test_statements_keep_the_order_of_the_base(self) -> None:
        base = (
            "create table if not exists public.a (id int);\n"
            "create table if not exists public.b (id int);\n"
            "create index if not exists a_idx on public.a (id);\n"
            "alter table public.b enable row level security;\n"
            "alter table public.a enable row level security;\n"
        )
        out = self.only("a", base)
        self.assertLess(out.index("create table"), out.index("create index"))
        self.assertLess(out.index("create index"), out.index("alter table"))
        self.assertNotIn("lf.b", out)

    def test_trailing_comments_after_the_last_statement_are_ignored(self) -> None:
        base = "create table if not exists public.t (id int);\n-- the end; really\n"
        out = self.only("t", base)
        self.assertIn("create table if not exists lf.t (id int);", out)
        self.assertNotIn("the end", out)


class FailClosedTests(unittest.TestCase):
    def test_unknown_tables_are_refused_in_one_line_that_names_the_known_ones(self) -> None:
        base = base_sql()
        for name in ("nope", "", "TRADES", "public.trades", "trades; drop table x", "shadow_entries\n", "alter"):
            with self.subTest(table=name):
                with self.assertRaises(ValueError) as caught:
                    forward_schema.table_sql("long_forward", name, base)
                self.assertNotIn("\n", str(caught.exception))
                self.assertIn("shadow_entries", str(caught.exception))

    def test_a_create_without_if_not_exists_is_refused(self) -> None:
        for base in (
            "create table public.t (id int);\n",
            "create table if not exists public.t (id int);\ncreate index t_idx on public.t (id);\n",
            "create table if not exists public.t (id int);\ncreate unique index t_idx on public.t (id);\n",
        ):
            with self.subTest(base=base):
                with self.assertRaises(ValueError) as caught:
                    forward_schema.table_sql("lf", "t", base)
                self.assertIn("if not exists", str(caught.exception))

    def test_syntax_the_splitter_does_not_understand_is_refused(self) -> None:
        bases = {
            "dollar quoting": "create function public.f() returns int as $$ select 1; $$ language sql;\n"
                              "create table if not exists public.t (id int);\n",
            "block comment": "/* a; b */ create table if not exists public.t (id int);\n",
            "unterminated string": "create table if not exists public.t (id int default 'x);\n",
            "unfinished statement": "create table if not exists public.t (id int)\n",
        }
        for label, base in bases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    forward_schema.table_sql("lf", "t", base)

    def test_a_bad_schema_name_is_refused(self) -> None:
        for name in ("public", "Long_Forward", "a;drop", "", "pg_catalog"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    forward_schema.table_sql(name, "shadow_entries", base_sql())

    def test_a_bare_public_left_after_the_rewrite_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            forward_schema.table_sql("lf", "t", "-- the public schema\ncreate table if not exists public.t (id int);\n")


class OnlyCliTests(unittest.TestCase):
    def test_unknown_table_is_one_error_line_and_exit_2(self) -> None:
        result = run_cli("--schema", "long_forward", "--only", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))
        self.assertIn("nope", lines[0])

    def test_only_reads_an_explicit_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.sql"
            path.write_text(
                "create table if not exists public.a (id int);\ncreate table if not exists public.b (id int);\n",
                encoding="utf-8",
            )
            result = run_cli("--schema", "lf", "--base", str(path), "--only", "b")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create table if not exists lf.b (id int);", result.stdout)
        self.assertNotIn("lf.a", result.stdout)

    def test_only_still_needs_a_schema(self) -> None:
        result = run_cli("--only", "shadow_entries")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_without_only_the_full_schema_is_printed(self) -> None:
        result = run_cli("--schema", "long_forward")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create schema if not exists long_forward;", result.stdout)
        self.assertIn("create table if not exists long_forward.trades (", result.stdout)


if __name__ == "__main__":
    unittest.main()
