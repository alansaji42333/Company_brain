"""Migration 0005 (drive_oauth_pendings): upgrade/downgrade round trip from
0004.

Runs the REAL Alembic chain against a disposable PostgreSQL database
(created and dropped by the test) when one is reachable — the authentic
project dialect. Skips with a clear reason on SQLite-only machines: the
chain's 0003 step (ALTER-FK on existing tables) has always required
Postgres (batch-mode or PG), and older migrations are deliberately not
modified to change that (out of scope per the session brief). No
DATABASE_URL env change leaks outside the subprocesses it spawns."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _reachable_postgres_url() -> str | None:
    """Return a libpq-verified URL for a disposable PG database, or None."""
    probe = subprocess.run(
        ["psql", "-d", "postgres", "-tAc", "select current_user, 1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        return None
    out = probe.stdout.strip().split("|")
    user = out[0].strip() if out else ""
    if not user or out[-1].strip() != "1":
        return None
    return f"postgresql+asyncpg://{user}@localhost:5432"


def _alembic(db_url: str, *args: str) -> str:
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"alembic {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout + proc.stderr  # alembic logs go to stderr


def _pg_tables(url: str, db_name: str) -> list[str]:
    proc = subprocess.run(
        ["psql", "-d", db_name, "-tAc",
         "SELECT tablename FROM pg_tables WHERE schemaname='public'"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _pg_columns(url: str, db_name: str, table: str) -> set[str]:
    proc = subprocess.run(
        ["psql", "-d", db_name, "-tAc",
         f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}'"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


@pytest.mark.skipif(_reachable_postgres_url() is None, reason="no disposable Postgres reachable")
def test_0005_upgrade_downgrade_round_trip_on_postgres():
    base_url = _reachable_postgres_url()
    assert base_url is not None
    db_name = f"cb_mig_probe_0005_{os.getpid()}"
    db_url = f"{base_url}/{db_name}"

    create = subprocess.run(["createdb", db_name], capture_output=True, text=True, timeout=30)
    assert create.returncode == 0, create.stderr
    try:
        out = _alembic(db_url, "upgrade", "0004_connector_configs")
        assert "0004" in out
        assert "org_connector_configs" in _pg_tables(db_url, db_name)

        out = _alembic(db_url, "upgrade", "head")
        assert "0005" in out
        tables = _pg_tables(db_url, db_name)
        assert "drive_oauth_pendings" in tables
        cols = _pg_columns(db_url, db_name, "drive_oauth_pendings")
        assert cols == {
            "id", "nonce_hash", "org_id", "user_id", "folder_id",
            "browser_secret_hash", "code_verifier_encrypted",
            "created_at", "expires_at", "consumed_at",
        }

        out = _alembic(db_url, "downgrade", "0004_connector_configs")
        assert "0004" in out
        assert "drive_oauth_pendings" not in _pg_tables(db_url, db_name)
        assert "org_connector_configs" in _pg_tables(db_url, db_name)  # neighbors intact

        # re-upgrade from the clean state also works
        _alembic(db_url, "upgrade", "head")
        assert "drive_oauth_pendings" in _pg_tables(db_url, db_name)
    finally:
        subprocess.run(["dropdb", "--if-exists", db_name], capture_output=True, text=True, timeout=30)
