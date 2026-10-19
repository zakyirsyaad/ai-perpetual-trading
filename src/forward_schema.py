"""Generate the SQL for the LONG forward test's own Supabase schema.

The second bot instance writes to its own schema (``SUPABASE_SCHEMA``) so its
rows never mix with the live SHORT bot's tables in ``public``. This module
derives that schema's SQL from ``supabase/schema.sql`` so the two cannot drift.

    PYTHONPATH=src python src/forward_schema.py --schema long_forward \
        > supabase/long_forward_schema.sql
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_BASE = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"

_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_MAX_NAME_LENGTH = 63  # PostgreSQL silently truncates longer identifiers
_RESERVED_NAMES = frozenset({"public", "information_schema"})
_QUALIFIED_PUBLIC = re.compile(r"(?<![\w.])public\.")
_BARE_PUBLIC = re.compile(r"\bpublic\b", re.IGNORECASE)


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
        f"grant usage on schema {schema} to service_role;\n"
        f"grant all on all tables in schema {schema} to service_role;\n"
        f"grant all on all sequences in schema {schema} to service_role;\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", required=True, help="target schema name")
    parser.add_argument(
        "--base", type=Path, default=DEFAULT_BASE, help="base SQL (default: supabase/schema.sql)"
    )
    args = parser.parse_args(argv)
    try:
        base_sql = args.base.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.base}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    try:
        output = schema_sql(args.schema, base_sql)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
