"""Apply supabase/migrations/*.sql to a remote Supabase project via the
Management API query endpoint.

Idempotent: applied files are recorded in public._migrations and skipped on
re-runs, matching the old project's convention.

Usage:
    SUPABASE_ACCESS_TOKEN=<personal access token> python apply_migrations.py
    SUPABASE_ACCESS_TOKEN=<token> SUPABASE_PROJECT_REF=<ref> python apply_migrations.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT.parent / "supabase" / "migrations"
PROJECT_REF = os.getenv("SUPABASE_PROJECT_REF", "yuqrbnwglpqjxhvbthef")
TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN", "").strip()
API = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"


def run_query(query: str):
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "migration-runner/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"Management API {e.code} for: {query[:200]}\n{e.reason}: {detail}")


def sq_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main():
    if not TOKEN:
        sys.exit("Set SUPABASE_ACCESS_TOKEN (dashboard -> Account -> Access Tokens).")
    if not MIGRATIONS.exists() or not list(MIGRATIONS.glob("*.sql")):
        sys.exit(f"No migration files in {MIGRATIONS}")

    run_query(
        "create table if not exists public._migrations ("
        "  version text primary key, applied_at timestamptz not null default now()"
        ");"
    )
    applied = {r["version"] for r in run_query("select version from public._migrations;")}

    for path in sorted(MIGRATIONS.glob("*.sql")):
        version = path.stem
        if version in applied:
            print(f"skip  {version} (already applied)")
            continue
        sql = path.read_text(encoding="utf-8")
        run_query(sql)
        run_query(
            "insert into public._migrations (version, applied_at) values (%s, %s)"
            % (sq_literal(version), sq_literal(datetime.now(timezone.utc).isoformat()))
        )
        print(f"apply {version}")

    tables = [
        r["table_name"]
        for r in run_query(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by table_name;"
        )
    ]
    print("public tables:", ", ".join(tables))


if __name__ == "__main__":
    main()