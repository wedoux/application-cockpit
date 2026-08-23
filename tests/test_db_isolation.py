import hashlib
import sqlite3

import pytest

import app
import db as dbmod
from conftest import REAL_DB_PATH, RealDatabaseAccessError


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guard_blocks_direct_access_to_real_db():
    """Prove the guard itself fires, not just that it's present in conftest."""
    with pytest.raises(RealDatabaseAccessError):
        sqlite3.connect(str(REAL_DB_PATH))


def test_full_app_exercise_leaves_real_cockpit_db_untouched():
    """Prove the isolation, don't just assert it. Exercise real routes that
    write (add a role, update it), confirm the writes actually landed
    (against the isolated temp DB — proving isolation isn't just "nothing
    happened"), then hash the real cockpit.db before and after and require
    them identical.

    A fresh clone has no cockpit.db at all yet (db.init_db() only creates
    it on first app start) — "prove it's untouched" is meaningless with
    nothing there to touch, so skip rather than fail on a missing file."""
    if not REAL_DB_PATH.exists():
        pytest.skip("no real cockpit.db on disk yet — nothing to prove untouched")
    before = _hash(REAL_DB_PATH)

    client = app.app.test_client()
    r = client.post("/api/roles", json={
        "company": "Isolation Test Co", "title": "Isolation Test Role",
        "jd_text": "A test job description, long enough to pass the length checks in place.",
    })
    assert r.status_code == 200, r.get_json()
    role_id = r.get_json()["role_id"]

    conn = dbmod.connect()
    row = conn.execute("SELECT company FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row is not None and row["company"] == "Isolation Test Co"  # isolated DB actually used

    after = _hash(REAL_DB_PATH)
    assert before == after, "the real cockpit.db changed during an isolated test run"


def test_before_request_creates_schema_on_a_never_initialized_db(tmp_path, monkeypatch):
    """The gap this guards: if app.app is ever served without main() having
    called init_db() first (a WSGI server that imports app.app directly and
    never runs main()), the first DB-touching request used to 500 with "no
    such table" instead of the schema just existing — see app.py's
    _ensure_db_ready. Point DB_PATH at a file that has genuinely never been
    initialized (conftest's own isolated_db fixture already calls init_db()
    on its temp DB, which would mask this if reused here) and confirm a
    fresh process's first request creates the schema itself, no main()
    required."""
    never_initialized = tmp_path / "genuinely_fresh.db"
    monkeypatch.setattr(dbmod, "DB_PATH", never_initialized)
    monkeypatch.setattr(app, "_db_ready", False)
    assert not never_initialized.exists()

    client = app.app.test_client()
    r = client.get("/api/roles")
    assert r.status_code == 200, r.get_json()

    conn = sqlite3.connect(never_initialized)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "roles" in tables
