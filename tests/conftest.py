"""
Test-wide database isolation.
==============================
Every test gets its own throwaway SQLite file, automatically — this must not
depend on a test author remembering to redirect anything. A real bug shipped
earlier in this build because `db.connect(db_path=DB_PATH)` bound its default
at import time, so `dbmod.DB_PATH = <scratch path>` silently did nothing and
"isolated" tests wrote to the real cockpit.db. connect() is now fixed to
resolve DB_PATH at call time (see db.py), which is what makes the
monkeypatch below actually work — but this fixture also patches
sqlite3.connect directly as a second, independent layer: even if some future
code opens the database without going through db.connect() at all, it still
can't reach the real file during a test run.
"""

import sqlite3
from pathlib import Path

import pytest

import app as app_module
import db as dbmod
import generation

# Captured once, at collection time, before any fixture has had a chance to
# monkeypatch db.DB_PATH — this is the one true path to protect.
REAL_DB_PATH = Path(dbmod.DB_PATH).resolve()


class RealDatabaseAccessError(RuntimeError):
    """Raised when anything — test code or app code — tries to open the real
    cockpit.db during a test run. A plain exception rather than pytest.fail()
    so the guard itself is directly testable (see test_db_isolation.py)."""


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    temp_db = tmp_path / "test_cockpit.db"
    monkeypatch.setattr(dbmod, "DB_PATH", temp_db)
    dbmod.init_db(temp_db)

    real_sqlite_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if isinstance(database, (str, Path)) and Path(database).resolve() == REAL_DB_PATH:
            raise RealDatabaseAccessError(
                f"Tried to open the real database at {REAL_DB_PATH}. Every test "
                "must go through the isolated temp DB — this should be "
                "impossible; if it fires, something is bypassing db.connect()."
            )
        return real_sqlite_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield temp_db


@pytest.fixture(autouse=True)
def isolated_applications_dir(tmp_path, monkeypatch):
    """PDF export (Phase 3b) writes files under app.APPLICATIONS_DIR — same
    isolation principle as the DB, so a test that downloads a document never
    writes into the real applications/ folder."""
    monkeypatch.setattr(app_module, "APPLICATIONS_DIR", tmp_path / "applications")


@pytest.fixture(autouse=True)
def isolated_backup_dir(tmp_path, monkeypatch):
    """db.backup_db() writes into db.BACKUP_DIR — same isolation principle as
    the DB itself, so a test that triggers a bulk-mutation path never writes
    into the real backups/ folder or prunes a real snapshot."""
    monkeypatch.setattr(dbmod, "BACKUP_DIR", tmp_path / "backups")


class RealApiCallError(RuntimeError):
    """Raised when a test reaches for a live Anthropic client. Same shape and
    same reasoning as RealDatabaseAccessError: a plain exception rather than
    pytest.fail(), so the guard itself is directly testable."""


@pytest.fixture(autouse=True)
def no_live_api_calls(monkeypatch):
    """No test may build a real Anthropic client.

    Until the numeric repair ladder there was one door to the API —
    generation.generate — and every test that needed it monkeypatched that
    one function, so nothing had to enforce this. The repair rung added a
    second door (generation.repair_numbers), and a test that forgot to patch
    it would have sent draft text to the API and been charged for it
    silently, because the ladder catches a failed repair and climbs rather
    than raising.

    A test that wants the repair path overrides generation.client_factory
    with its own fake, which is a deliberate act rather than an omission.
    """
    def guarded():
        raise RealApiCallError(
            "A test tried to build a live Anthropic client. Patch "
            "generation.generate, or generation.client_factory for the "
            "repair path, with a fake instead."
        )

    monkeypatch.setattr(generation, "client_factory", guarded)
