import argparse
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from hub.db import migrate

SECRETS = Path("/run/secrets")


def owner_connection():
    return make_conninfo(
        host="db",
        user="hub_owner",
        dbname="knowledgehub",
        password=(SECRETS / "postgres_password").read_text(encoding="utf-8").strip(),
    )


def provision(url):
    runtime = conninfo_to_dict((SECRETS / "database_url").read_text(encoding="utf-8").strip())
    password = runtime.get("password")
    if runtime.get("user") != "hub_runtime" or not password or len(password) < 24:
        raise ValueError(
            "Runtime connection requires hub_runtime and a random password of at least 24 characters"
        )
    with psycopg.connect(url) as db:
        db.execute("SELECT pg_advisory_xact_lock(3626816)")
        role = db.execute("SELECT 1 FROM pg_roles WHERE rolname = 'hub_runtime'").fetchone()
        operation = sql.SQL("ALTER ROLE") if role else sql.SQL("CREATE ROLE")
        db.execute(
            sql.SQL(
                "{} hub_runtime LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS "
                "PASSWORD {}"
            ).format(operation, sql.Literal(password))
        )
        db.execute("GRANT USAGE ON SCHEMA public TO hub_runtime")
        db.execute("""
            GRANT SELECT, INSERT, UPDATE, DELETE ON users, sessions, login_flows, groups, memberships,
                sources, source_groups, versions, chunks, jobs, question_limits, login_limits,
                audit_events, worker_stats
            TO hub_runtime
        """)


def main():
    parser = argparse.ArgumentParser(description="Knowledge Hub deployment operations")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("migrate")
    grant = sub.add_parser("grant-admin")
    grant.add_argument("subject")
    revoke = sub.add_parser("disable-admin", help="Disable an administrator and revoke their sessions")
    revoke.add_argument("subject")
    args = parser.parse_args()
    if args.operation != "migrate":
        issuer = os.environ.get("OIDC_ISSUER", "")
        if not issuer or not 0 < len(args.subject) <= 255:
            parser.error("Configure OIDC_ISSUER and pass the exact identity-provider subject.")
    try:
        url = owner_connection()
        if args.operation == "migrate":
            migrate(url)
            provision(url)
        else:
            with psycopg.connect(url) as db:
                if args.operation == "grant-admin":
                    db.execute(
                        """
                        INSERT INTO users (issuer, subject, is_admin) VALUES (%s, %s, true)
                        ON CONFLICT (issuer, subject) DO UPDATE SET is_admin = true, enabled = true
                    """,
                        (issuer, args.subject),
                    )
                else:
                    disabled = db.execute(
                        """
                        UPDATE users SET is_admin = false, enabled = false
                        WHERE issuer = %s AND subject = %s AND (is_admin OR NOT enabled)
                        RETURNING id
                        """,
                        (issuer, args.subject),
                    ).fetchone()
                    if disabled is None:
                        raise SystemExit("No administrator matched the configured issuer and subject.")
                    # Use a fresh snapshot after the account update waits for any concurrent sign-in.
                    db.execute("DELETE FROM sessions WHERE user_id = %s", (disabled[0],))
            if args.operation == "disable-admin":
                print("Account disabled and sessions revoked.")
    except (psycopg.Error, OSError, ValueError):
        raise SystemExit("Deployment command failed. Check database and secret configuration.") from None


if __name__ == "__main__":
    main()
