"""Check every service's database migrations against a REAL PostgreSQL.

For each service it: applies the migrations, checks they produce exactly what the code's models
expect (no drift), rolls everything back, and applies it again. SQLite, which the unit tests use,
is much more forgiving than Postgres, so this is the check that matters.

    python scripts/check_migrations.py                       # Docker's Postgres on localhost:5432
    python scripts/check_migrations.py --port 5433           # if you moved it (POSTGRES_HOST_PORT)
    python scripts/check_migrations.py --only order payment

The databases (auth_db, order_db, ...) must already exist:
`docker compose up -d db-init` creates them.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ["auth", "user", "product", "inventory", "order", "payment", "notification"]


def alembic(service: str, url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT / "services" / service,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


def check(service: str, url: str) -> list[str]:
    problems = []
    steps = [
        ("apply migrations", ["upgrade", "head"]),
        ("match the models (no drift)", ["check"]),
        ("roll back", ["downgrade", "base"]),
        ("apply again", ["upgrade", "head"]),
    ]
    for name, args in steps:
        result = alembic(service, url, *args)
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
            problems.append(f"{name}: {last}")
            break  # later steps would only repeat the failure
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    parser.add_argument("--only", nargs="*", choices=SERVICES, help="check just these services")
    args = parser.parse_args()

    failed = False
    for service in args.only or SERVICES:
        url = (
            f"postgresql+asyncpg://{args.user}:{args.password}@{args.host}:{args.port}/{service}_db"
        )
        problems = check(service, url)
        print(f"{'FAIL' if problems else 'ok  '} {service}")
        for problem in problems:
            print(f"       {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
