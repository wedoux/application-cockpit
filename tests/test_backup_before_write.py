"""
Every bulk-mutation entry point calls db.backup_db() before it writes.
========================================================================
This coverage died with migrate.py (deleted 2026-08-19): test_backup.py
used to prove the "snapshot before write" contract via migrate.py's own
call site, and nothing replaced it when migrate.py went — a real gap
found by inspection, not by a failing test.

cockpit.db is gitignored: db.backup_db()'s snapshot is the only recovery
path for data if a bulk write goes wrong (git history / phase tags
protect code only, never data). This test proves the ordering — backup
called strictly before any write-shaped SQL — for every real entry point
that mutates roles in bulk, not just that backup_db() itself works in
isolation (already covered in test_backup.py).

Mechanism: db.connect (through which every entry point below eventually
gets its connection — some directly, gmail_sweep_apply.py's main()
internally) is wrapped so every call it returns records write-shaped SQL
(INSERT/UPDATE/DELETE/REPLACE) into a shared, ordered events list;
db.backup_db is wrapped the same way. If backup_db is ever removed, or
moved to after the first write, this fails.
"""
import json
import re
import sys

import pytest

import app
import db as dbmod
import gmail_sweep_apply as gsa
import merge_duplicate_roles as mdr
import reconcile_tracker as rt
import staging

_WRITE_SQL_RE = re.compile(r"^\s*(insert|update|delete|replace)\b", re.IGNORECASE)


class _OrderTrackingConn:
    """Wraps a real sqlite3.Connection (or db.Row-returning equivalent) and
    appends "write" to `events` the moment any write-shaped SQL executes.
    Delegates everything else untouched — same wrap-don't-subclass
    approach test_gmail_sweep_apply.py's _WriteGuardedConn uses, since
    sqlite3.Connection is a builtin type and can't be class-patched."""
    def __init__(self, real_conn, events):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_events", events)

    def execute(self, sql, *args, **kwargs):
        if _WRITE_SQL_RE.match(sql):
            self._events.append(("write", sql.strip()[:60]))
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def events(monkeypatch):
    """Patches db.connect and db.backup_db to record ordered events into
    the returned list. Every entry point below gets its connection through
    db.connect (directly, or internally as gmail_sweep_apply.main() does),
    so patching it here covers all of them uniformly."""
    log = []
    real_connect = dbmod.connect

    def tracking_connect(*a, **kw):
        return _OrderTrackingConn(real_connect(*a, **kw), log)

    def tracking_backup_db(reason, *a, **kw):
        log.append(("backup", reason))
        return dbmod.BACKUP_DIR / "fake-backup-for-ordering-test.db"

    monkeypatch.setattr(dbmod, "connect", tracking_connect)
    monkeypatch.setattr(dbmod, "backup_db", tracking_backup_db)
    return log


def _assert_backup_precedes_write(events):
    backup_indices = [i for i, (kind, _) in enumerate(events) if kind == "backup"]
    write_indices = [i for i, (kind, _) in enumerate(events) if kind == "write"]
    assert backup_indices, "backup_db was never called"
    assert write_indices, "no write-shaped SQL executed — test didn't exercise a real write"
    assert min(backup_indices) < min(write_indices), (
        f"backup_db must be called before the first write, got order: {events}"
    )


def test_scan_into_db_backs_up_before_writing(events, monkeypatch):
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: [{
        "company": "Acme", "portal": "greenhouse", "outcome": "ok", "error_detail": None,
        "raw_count": 1,
        "title_matches": [{"job_id": "order-test-1", "company": "Acme", "title": "Head of Design",
                            "location": "Geneva", "url": "https://x/1", "portal": "greenhouse",
                            "posted": "2026-08-20"}],
        "dropped_titles": [],
        "location_matches": [{"job_id": "order-test-1", "company": "Acme", "title": "Head of Design",
                               "location": "Geneva", "url": "https://x/1", "portal": "greenhouse",
                               "posted": "2026-08-20"}],
        "dropped_locations": [],
    }])
    app.scan_into_db()
    _assert_backup_precedes_write(events)


def test_gmail_sweep_apply_backs_up_before_writing(events, tmp_path, monkeypatch):
    conn = dbmod.connect()
    conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("order-test-2", "manual", "Acme", "Head of Design", "Leadership", "submitted",
         dbmod.today(), dbmod.today(), dbmod.now(), dbmod.now()),
    )
    conn.commit()
    conn.close()

    updates_file = tmp_path / "updates.json"
    updates_file.write_text(json.dumps([{"company": "Acme", "title": "Head of Design",
                                          "status": "interview"}]))
    events.clear()  # setup insert above is fixture data, not part of what's under test
    old_argv = sys.argv
    sys.argv = ["gmail_sweep_apply.py", str(updates_file), "--apply"]
    try:
        gsa.main()
    finally:
        sys.argv = old_argv
    _assert_backup_precedes_write(events)


def test_merge_duplicate_roles_backs_up_before_writing(events):
    conn = dbmod.connect()
    ts = dbmod.now()
    t_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, notes, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("order-test-tracker", "manual", "Acme", "Head of Design", "Leadership", "sourced", None,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    s_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, notes, url, portal, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("order-test-scanner", "auto", "Acme", "Head of Design", "Leadership", "sourced", None,
         "https://x/2", "greenhouse", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    t = conn.execute("SELECT * FROM roles WHERE id = ?", (t_id,)).fetchone()
    s = conn.execute("SELECT * FROM roles WHERE id = ?", (s_id,)).fetchone()

    events.clear()  # setup inserts above are fixture data, not part of what's under test
    mdr.apply_merge(conn, [(t, s)])
    _assert_backup_precedes_write(events)


def test_reconcile_tracker_apply_plan_backs_up_before_writing(events):
    plan = {
        "merges": [], "fuzzies": [], "conflicts": [],
        "news": [{
            "_slug": "order-test-reconcile", "company": "Acme", "title": "Head of Design",
            "_category": "Leadership", "score": None, "url": "https://x/3", "status": "sourced",
            "interest": None, "channel": None, "next_action": None, "applied_date": None,
            "due": None, "notes": None,
        }],
    }
    rt.apply_plan(plan)
    _assert_backup_precedes_write(events)


def test_staging_ingest_backs_up_before_writing(events, tmp_path, monkeypatch):
    """The ingest panel is a bulk-mutation entry point like any other — it
    can create a dozen roles from one click, and cockpit.db is gitignored,
    so its snapshot is the only way back."""
    staged_dir = tmp_path / "staging"
    staged_dir.mkdir()
    monkeypatch.setattr(staging, "STAGING_DIR", staged_dir)
    path = staged_dir / "creates.json"
    path.write_text(json.dumps([{"company": "Order Test Co", "title": "Head of Design",
                                  "url": "https://x/order-test"}]))

    staging.ingest(path)
    _assert_backup_precedes_write(events)
