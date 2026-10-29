"""Generate the SQL for the LONG forward test's own Supabase schema.

The second bot instance writes to its own schema (``SUPABASE_SCHEMA``) so its
rows never mix with the live SHORT bot's tables in ``public``. This module
derives that schema's SQL from ``supabase/schema.sql`` so the two cannot drift.

    PYTHONPATH=src python src/forward_schema.py --schema long_forward \
        > supabase/long_forward_schema.sql

``--only TABLE`` prints just one table's statements (its create table, indexes and
row level security, then the schema's grants) for a schema that already exists. That
is how a table added to ``schema.sql`` after the schema was first created reaches it:

    PYTHONPATH=src python src/forward_schema.py --schema long_forward \
        --only shadow_entries > supabase/long_forward_shadow_entries.sql
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"

_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_MAX_NAME_LENGTH = 63  # PostgreSQL silently truncates longer identifiers
# Never a target: `public` is the live bot's schema, the rest are Postgres and Supabase system schemas (the
# generated grants would hand them to service_role).
_RESERVED_NAMES = frozenset({
    "public", "information_schema", "auth", "storage", "realtime", "extensions", "graphql", "graphql_public",
    "vault", "cron", "net", "pgsodium", "pgsodium_masks", "supabase_functions", "supabase_migrations", "pgbouncer",
})
_QUALIFIED_PUBLIC = re.compile(r"(?<![\w.])public\.")
_BARE_PUBLIC = re.compile(r"\bpublic\b", re.IGNORECASE)

_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]*\$")
_FIRST_TABLE_REFERENCE = re.compile(r"(?<![\w.])public\.([A-Za-z_][A-Za-z0-9_]*)")
_CREATE_TABLE = re.compile(
    r"\s*create\s+table\s+(?:if\s+not\s+exists\s+)?public\.([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
# A statement that cannot be run twice: a create or an add column whose keywords are not directly followed by
# `if not exists`. Anchored at the keywords, so the phrase inside a default or a comment proves nothing. The lookahead
# also refuses a space, so `\s+` cannot give one back and then see `if not exists` after it (two spaces, or a newline
# and an indent, before the phrase would otherwise read as a missing `if not exists`).
_NOT_RERUNNABLE = re.compile(
    r"\s*(?:create\s+(?:unique\s+)?(?:table|index)|alter\s+table\s+\S+\s+add\s+column)\s+(?!\s|if\s+not\s+exists\b)",
    re.IGNORECASE,
)


def _validate_schema_name(schema: str) -> None:
    if (
        not _SCHEMA_NAME.fullmatch(schema)
        or len(schema) > _MAX_NAME_LENGTH
        or schema in _RESERVED_NAMES
        or schema.startswith("pg_")
    ):
        raise ValueError(
            f"schema name {schema!r} refused: use [a-z_][a-z0-9_]* up to "
            f"{_MAX_NAME_LENGTH} characters, not 'public', 'information_schema' "
            "or a 'pg_' prefix"
        )


def _grants(schema: str) -> str:
    return (
        f"grant usage on schema {schema} to service_role;\n"
        f"grant all on all tables in schema {schema} to service_role;\n"
        f"grant all on all sequences in schema {schema} to service_role;\n"
    )


def schema_sql(schema: str, base_sql: str) -> str:
    """Return ``base_sql`` re-homed from ``public`` into ``schema``.

    Fails closed: a base with no ``public.`` reference, or one that still
    mentions ``public`` after the rewrite, is refused rather than emitted.
    """
    _validate_schema_name(schema)
    body, count = _QUALIFIED_PUBLIC.subn(f"{schema}.", base_sql)
    if count == 0:
        raise ValueError("base SQL has no qualified public. reference to rewrite")
    if _BARE_PUBLIC.search(body):
        raise ValueError("base SQL still mentions public after the rewrite")
    return (
        "-- Generated from supabase/schema.sql by src/forward_schema.py; "
        "do not edit by hand.\n"
        f"-- Expose '{schema}' under Settings > API > Exposed schemas "
        "so PostgREST serves it.\n"
        f"create schema if not exists {schema};\n"
        "\n"
        f"{body.rstrip()}\n"
        "\n"
        f"{_grants(schema)}"
    )


@dataclass(frozen=True)
class _Statement:
    text: str  # as written, the comment lines above it included, without the closing semicolon
    code: str  # the same without comments
    ends_in_comment: bool = False  # a `--` comment runs up to the closing semicolon, which then needs its own line

    @property
    def table(self) -> str | None:
        """The table the statement is about: the first ``public.`` name in its code.

        The first name, not any name: ``create table public.child (... references public.parent ...)``
        is the child's. Comments are not code, so a comment naming another table moves nothing.
        """
        match = _FIRST_TABLE_REFERENCE.search(self.code)
        return match.group(1) if match else None


def _after_quoted(sql: str, start: int) -> int:
    """Index just past the quoted text that opens at ``start``.

    SQL escapes a quote by doubling it, which needs no special case here: ``'it''s'`` reads as the quoted
    pieces ``'it'`` and ``'s'`` side by side, and together they cover the same characters.
    """
    end = sql.find(sql[start], start + 1)
    if end == -1:
        raise ValueError("base SQL has an unterminated quoted string")
    return end + 1


def _split_statements(base_sql: str) -> list[_Statement]:
    """Split ``base_sql`` at the semicolons that end a statement.

    ``schema.sql`` is plain DDL whose comments hold semicolons, so a plain split would cut statements
    in the wrong places: this walks the text instead, skipping ``--`` comments and quoted text and
    counting parentheses. What it cannot read (dollar quoting, block comments, an unterminated string,
    a statement with no closing semicolon) is refused, never guessed at.
    """
    if _DOLLAR_QUOTE.search(base_sql):
        raise ValueError("base SQL uses dollar quoting, which --only cannot split safely")
    if "/*" in base_sql:
        raise ValueError("base SQL uses a block comment, which --only cannot split safely")
    statements: list[_Statement] = []
    text: list[str] = []
    code: list[str] = []
    depth = 0
    position = 0
    ends_in_comment = False
    while position < len(base_sql):
        char = base_sql[position]
        if base_sql.startswith("--", position):
            end = base_sql.find("\n", position)
            end = len(base_sql) if end == -1 else end
            text.append(base_sql[position:end])
            position = end
            ends_in_comment = True
        elif char in "'\"":
            end = _after_quoted(base_sql, position)
            text.append(base_sql[position:end])
            code.append(base_sql[position:end])
            position = end
            ends_in_comment = False
        elif char == ";" and depth == 0:
            statements.append(_Statement("".join(text).strip(), "".join(code), ends_in_comment))
            text, code = [], []
            position += 1
            ends_in_comment = False
        else:
            text.append(char)
            code.append(char)
            depth += (char == "(") - (char == ")")
            position += 1
            ends_in_comment = ends_in_comment and char.isspace()
    if "".join(code).strip():
        raise ValueError("base SQL ends inside a statement (no closing semicolon)")
    return statements


def table_sql(schema: str, table: str, base_sql: str) -> str:
    """Return just ``table``'s statements from ``base_sql``, re-homed into ``schema``, then the schema's grants.

    For a schema that already exists and lacks only this table. Statements keep the order of the base.
    Fails closed like ``schema_sql``: an unknown table, a create that is not safe to run again, SQL the
    splitter cannot read, or a ``public`` left after the rewrite is refused rather than emitted.
    """
    _validate_schema_name(schema)
    statements = _split_statements(base_sql)
    known = [match.group(1) for statement in statements if (match := _CREATE_TABLE.match(statement.code))]
    if table not in known:
        raise ValueError(f"unknown table {table!r}; schema.sql creates: {', '.join(known)}")
    for statement in statements:
        unplaceable = statement.table is None and (
            _BARE_PUBLIC.search(statement.code) or any(re.search(rf"\b{name}\b", statement.code) for name in known)
        )
        if unplaceable:
            raise ValueError("a statement names `public` or a table without a qualified public.<table> name, so --only cannot place it")
    chosen = [statement for statement in statements if statement.table == table]
    for statement in chosen:
        if _NOT_RERUNNABLE.match(statement.code):
            raise ValueError(f"a statement for {table!r} lacks 'if not exists' (create or add column), so it cannot be run again safely")
    pieces = (f"{statement.text}{chr(10) if statement.ends_in_comment else ''};" for statement in chosen)
    body, _ = _QUALIFIED_PUBLIC.subn(f"{schema}.", "\n\n".join(pieces))
    if _BARE_PUBLIC.search(body):
        raise ValueError("base SQL still mentions public after the rewrite")
    return (
        f"-- Generated from supabase/schema.sql by src/forward_schema.py (--only {table}); do not edit by hand.\n"
        f"-- Adds the {table} table to the existing '{schema}' schema. The generator refused any table or index "
        "creation, or first add column, without `if not exists`; other statements are copied as written, so check "
        "them before running the file twice.\n"
        "\n"
        f"{body}\n"
        "\n"
        f"{_grants(schema)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", required=True, help="target schema name")
    parser.add_argument(
        "--base", type=Path, default=DEFAULT_BASE, help="base SQL (default: supabase/schema.sql)"
    )
    parser.add_argument(
        "--only",
        metavar="TABLE",
        help="print just this table's statements and the schema's grants, for a schema that already exists",
    )
    args = parser.parse_args(argv)
    try:
        base_sql = args.base.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.base}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    try:
        if args.only is None:
            output = schema_sql(args.schema, base_sql)
        else:
            output = table_sql(args.schema, args.only, base_sql)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
