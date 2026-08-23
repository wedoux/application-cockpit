"""
db.backup_db() — pre-mutation snapshots.
=========================================
cockpit.db is gitignored, so a snapshot taken before any bulk write is the
only recovery path for data (git history / phase tags only protect code).
These tests cover the helper itself. Real callers (app.py's scan path,
gmail_sweep_apply.py, merge_duplicate_roles.py, reconcile_tracker.py) each
call backup_db() before writing, but none of that is asserted end-to-end
here — this file previously proved it via migrate.py (deleted 2026-08-19,
its Phase 0 job long done); no replacement entry-point test was written.
"""

import os
import re

import db as dbmod

NAME_RE = re.compile(r"^cockpit-\d{8}-\d{6}-[A-Za-z0-9_-]+\.db$")


def test_backup_db_writes_timestamped_copy():
    dest = dbmod.backup_db("unit-test")
    assert dest is not None
    assert dest.exists()
    assert NAME_RE.match(dest.name), dest.name
    assert dest.read_bytes() == dbmod.DB_PATH.read_bytes()


def test_backup_db_sanitizes_reason():
    dest = dbmod.backup_db("weird reason/with slashes!!")
    assert dest.exists()
    assert "/" not in dest.stem.split("-", 3)[-1]


def test_backup_db_noop_without_a_database(tmp_path):
    missing = tmp_path / "nope.db"
    assert dbmod.backup_db("whatever", db_path=missing) is None
    assert not dbmod.BACKUP_DIR.exists()


def test_prune_backups_retains_only_the_newest_n(monkeypatch):
    monkeypatch.setattr(dbmod, "BACKUP_RETAIN", 5)
    dbmod.BACKUP_DIR.mkdir(exist_ok=True)
    for i in range(12):
        f = dbmod.BACKUP_DIR / f"cockpit-fake-{i:02d}.db"
        f.write_bytes(b"x")
        os.utime(f, (i, i))  # stagger mtimes so ordering is deterministic

    dbmod._prune_backups()

    remaining = sorted(dbmod.BACKUP_DIR.glob("cockpit-*.db"))
    assert len(remaining) == 5
    assert [f.name for f in remaining] == [f"cockpit-fake-{i:02d}.db" for i in range(7, 12)]


