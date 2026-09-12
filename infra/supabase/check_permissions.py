"""Verify Supabase directory SQL against an owned, empty local PostgreSQL fixture."""

import argparse
import json
from pathlib import Path

import psycopg

from creditlens.corpus import build_demo_borrowers


def verify(port: int) -> dict:
    """Use fixture-only credentials and real role switches; never connect to a cloud database."""
    expected = [tuple(b.model_dump().values()) for b in build_demo_borrowers()]
    migration = Path(__file__).with_name("directory.sql").read_text(encoding="utf-8")
    checks: list[str] = []
    with psycopg.connect(
        host="127.0.0.1",
        port=port,
        dbname="creditlens_test",
        user="postgres",
        password="creditlens-test-only",  # noqa: S106 - public disposable loopback fixture
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from pg_tables where schemaname='public'")
            if cursor.fetchone() != (0,):
                raise ValueError("Use a fresh owned fixture with an empty public schema")
            cursor.execute("create role anon nologin; create role authenticated nologin")
            cursor.execute(migration)
            for role in ("anon", "authenticated"):
                cursor.execute(psycopg.sql.SQL("set role {}").format(psycopg.sql.Identifier(role)))
                cursor.execute(
                    "select borrower_id,name,industry from public.creditlens_demo_borrowers "
                    "order by borrower_id"
                )
                if cursor.fetchall() != expected:
                    raise AssertionError("Directory differs from canonical synthetic fixture")
                checks.append(f"{role}: canonical five rows readable")
                for statement in (
                    "select published from public.creditlens_demo_borrowers",
                    "insert into public.creditlens_demo_borrowers (borrower_id,name,industry) "
                    "values ('borrower-001','x','x')",
                    "update public.creditlens_demo_borrowers set name='x'",
                    "delete from public.creditlens_demo_borrowers",
                    "truncate public.creditlens_demo_borrowers",
                    "alter table public.creditlens_demo_borrowers disable row level security",
                ):
                    try:
                        cursor.execute(statement)
                    except psycopg.errors.InsufficientPrivilege:
                        checks.append(f"{role}: {statement.split()[0]} denied")
                    else:
                        raise AssertionError(f"Unexpected privilege for {role}: {statement}")
                cursor.execute("reset role")
            cursor.execute(
                "update public.creditlens_demo_borrowers set published=false "
                "where borrower_id='borrower-005'"
            )
            for role in ("anon", "authenticated"):
                cursor.execute(psycopg.sql.SQL("set role {}").format(psycopg.sql.Identifier(role)))
                cursor.execute(
                    "select borrower_id from public.creditlens_demo_borrowers order by borrower_id"
                )
                if cursor.fetchall() != [(row[0],) for row in expected[:4]]:
                    raise AssertionError("Unpublished fixture row bypassed RLS")
                checks.append(f"{role}: unpublished row excluded by RLS")
                cursor.execute("reset role")
            cursor.execute("update public.creditlens_demo_borrowers set published=true")
    return {
        "passed": True,
        "checks": checks,
        "scope": "Local PostgreSQL permissions; no managed Supabase deployment",
    }


def main() -> None:
    """Retain an explicit report without a database URL or credential material."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a fresh evidence output")
    report = verify(args.port)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
