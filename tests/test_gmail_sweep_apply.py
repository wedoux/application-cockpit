import json
import re

import db as dbmod
import gmail_sweep_apply as gsa

_WRITE_SQL_RE = re.compile(r"^\s*(insert|update|delete|replace)\b", re.IGNORECASE)


class _WriteGuardedConn:
    """Wraps a real sqlite3.Connection and raises if any write-shaped SQL
    is ever executed through it. sqlite3.Connection is a builtin type and
    can't be class-patched directly (TypeError: immutable type), so this
    wraps the object db.connect() returns instead — every other method/
    attribute (commit, close, row_factory, ...) delegates untouched."""
    def __init__(self, real_conn):
        object.__setattr__(self, "_real", real_conn)

    def execute(self, sql, *args, **kwargs):
        if _WRITE_SQL_RE.match(sql):
            raise AssertionError(f"write statement executed during --create without --apply: {sql!r}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _insert_role(company, title, status="submitted", notes=None):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, notes, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}", "manual", company, title, "Leadership", status, notes,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def test_dry_run_writes_nothing():
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted")
    updates = [{"company": "Acme Corp", "title": "Head of Design", "status": "rejected", "note": "rejected 12.08"}]

    conn = dbmod.connect()
    matched, unmatched, invalid = gsa.plan(conn, updates)
    conn.close()

    assert len(matched) == 1
    assert matched[0][2]["status"] == ("submitted", "rejected")

    conn = dbmod.connect()
    row = conn.execute("SELECT status FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "submitted", "plan() must never write — that's apply_plan()'s job"


def test_apply_writes_status_and_appends_note():
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted", notes="original note")
    updates = [{"company": "Acme Corp", "title": "Head of Design", "status": "rejected", "note": "rejected 12.08"}]

    conn = dbmod.connect()
    matched, unmatched, invalid = gsa.plan(conn, updates)
    gsa.apply_plan(conn, matched)
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT status, notes FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "rejected"
    assert row["notes"] == "original note [rejected 12.08]"


def test_matching_is_normalized_company_and_title():
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted")
    # Different case, punctuation, whitespace — same normalized identity.
    updates = [{"company": "ACME   corp.", "title": "head-of-design", "status": "rejected"}]

    conn = dbmod.connect()
    matched, unmatched, invalid = gsa.plan(conn, updates)
    conn.close()

    assert len(matched) == 1
    assert matched[0][1]["id"] == role_id


def test_unmatched_entries_are_reported_not_created():
    updates = [{"company": "Totally New Company", "title": "Some Role", "status": "submitted"}]

    conn = dbmod.connect()
    n_before = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    matched, unmatched, invalid = gsa.plan(conn, updates)
    n_after = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()

    assert matched == []
    assert len(unmatched) == 1
    assert n_after == n_before, "plan() must never insert a role"


def test_invalid_status_is_rejected_not_applied():
    _insert_role("Acme Corp", "Head of Design", status="submitted")
    updates = [{"company": "Acme Corp", "title": "Head of Design", "status": "not-a-real-status"}]

    conn = dbmod.connect()
    matched, unmatched, invalid = gsa.plan(conn, updates)
    conn.close()

    assert matched == []
    assert len(invalid) == 1


def test_no_op_update_is_not_counted_as_a_change():
    # Same status, no note — nothing to apply, even though it matched.
    _insert_role("Acme Corp", "Head of Design", status="submitted")
    updates = [{"company": "Acme Corp", "title": "Head of Design", "status": "submitted"}]

    conn = dbmod.connect()
    matched, unmatched, invalid = gsa.plan(conn, updates)
    conn.close()

    assert matched == []
    assert unmatched == []
    assert invalid == []


def test_cli_end_to_end_dry_run_then_apply(tmp_path, capsys):
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted")
    updates_file = tmp_path / "updates.json"
    updates_file.write_text(json.dumps([
        {"company": "Acme Corp", "title": "Head of Design", "status": "rejected", "note": "rejected 12.08"}
    ]))

    import sys
    old_argv = sys.argv

    sys.argv = ["gmail_sweep_apply.py", str(updates_file)]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out

    conn = dbmod.connect()
    row = conn.execute("SELECT status FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "submitted", "dry run must not have written"

    sys.argv = ["gmail_sweep_apply.py", str(updates_file), "--apply"]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "Applied 1 update" in out

    conn = dbmod.connect()
    row = conn.execute("SELECT status FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "rejected"


# ------------------------------------------------------------------
# --create mode
# ------------------------------------------------------------------

def test_create_plan_accepts_a_genuinely_new_role():
    creates = [{"company": "New Co", "title": "Head of Design", "url": "https://example.com/job/1",
                "source_email_date": "2026-08-20", "suggested_category": "Leadership"}]

    conn = dbmod.connect()
    n_before = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    n_after = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()

    assert len(to_create) == 1
    assert to_create[0]["company"] == "New Co"
    assert n_after == n_before, "plan_creates() must never write — that's apply_creates()'s job"


def test_create_requires_company_and_url():
    creates = [
        {"title": "No company", "url": "https://example.com/1"},
        {"company": "No URL Co", "title": "Missing url"},
    ]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    conn.close()

    assert to_create == []
    assert len(invalid) == 2


def test_create_does_not_require_a_title():
    creates = [{"company": "New Co", "url": "https://example.com/job/1"}]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    conn.close()

    assert invalid == []
    assert len(to_create) == 1
    assert to_create[0]["title"] is None


def test_create_is_blocked_by_an_existing_matching_role():
    _insert_role("Acme Corp", "Head of Design", status="rejected")
    creates = [{"company": "Acme Corp", "title": "Head of Design", "url": "https://example.com/job/2"}]

    conn = dbmod.connect()
    n_before = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    n_after = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()

    assert to_create == []
    assert len(blocked) == 1
    assert n_after == n_before


def test_create_force_overrides_the_block():
    role_id = _insert_role("Acme Corp", "Head of Design", status="rejected")
    creates = [{"company": "Acme Corp", "title": "Head of Design", "url": "https://example.com/job/2"}]

    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates, force=True)
    conn.close()

    assert blocked == []
    assert len(to_create) == 1
    assert to_create[0]["forced_past"] == role_id


def test_apply_creates_writes_expected_fields():
    creates = [{"company": "New Co", "title": "Head of Design", "url": "https://example.com/job/1",
                "source_email_date": "2026-08-20", "suggested_category": "Leadership"}]

    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    gsa.apply_creates(conn, to_create)
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT * FROM roles WHERE company = 'New Co'").fetchone()
    conn.close()
    assert row["status"] == "sourced"
    assert row["source"] == "sweep"
    assert row["category"] == "Leadership"
    assert row["url"] == "https://example.com/job/1"
    assert "2026-08-20" in row["notes"]


def test_apply_creates_note_reflects_the_actual_source_channel():
    """The manual careers-page sweep feeds this same --create path now —
    the provenance note must say so, not assert an email that doesn't
    exist."""
    creates = [{"company": "Careers Co", "title": "Head of Design",
                "url": "https://example.com/job/1", "source_channel": "careers_page"}]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    gsa.apply_creates(conn, to_create)
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT notes FROM roles WHERE company = 'Careers Co'").fetchone()
    conn.close()
    assert "manual careers-page sweep" in row["notes"]
    assert "Gmail" not in row["notes"]


def test_apply_creates_note_infers_gmail_from_a_source_email_date():
    """Backward-compat: existing Gmail-sweep callers don't set
    source_channel explicitly, only source_email_date — that's still a
    real inference (an email date implies email), not a default."""
    creates = [{"company": "Email Co", "title": "Head of Design",
                "url": "https://example.com/job/1", "source_email_date": "2026-08-20"}]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    gsa.apply_creates(conn, to_create)
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT notes FROM roles WHERE company = 'Email Co'").fetchone()
    conn.close()
    assert "Gmail sweep" in row["notes"]


def test_apply_creates_note_stays_generic_with_no_source_evidence_at_all():
    """No source_channel, no source_email_date — the note must not claim
    a channel it has no evidence for."""
    creates = [{"company": "Unknown Co", "title": "Head of Design",
                "url": "https://example.com/job/1"}]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    gsa.apply_creates(conn, to_create)
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT notes FROM roles WHERE company = 'Unknown Co'").fetchone()
    conn.close()
    assert row["notes"].startswith("Found via sweep (--create).")
    assert "Gmail" not in row["notes"]
    assert "careers-page" not in row["notes"]


def test_created_rows_are_never_auto_expired_by_the_scanner(monkeypatch):
    """source='sweep' must get the same never-auto-expired protection
    source='manual' and source='tracker' already have — scan_into_db only
    ever expires source='auto' rows."""
    import app as app_module

    creates = [{"company": "New Co", "title": "Head of Design", "url": "https://example.com/job/1"}]
    conn = dbmod.connect()
    to_create, blocked, invalid = gsa.plan_creates(conn, creates)
    gsa.apply_creates(conn, to_create)
    conn.close()

    # A scan that finds nothing at all for "New Co" must not touch the row.
    monkeypatch.setattr(app_module, "scan_all_companies_detailed", lambda: [])
    app_module.scan_into_db()

    conn = dbmod.connect()
    row = conn.execute("SELECT status FROM roles WHERE company = 'New Co'").fetchone()
    conn.close()
    assert row["status"] == "sourced"


def test_create_cli_end_to_end_dry_run_then_apply(tmp_path, capsys):
    creates_file = tmp_path / "new_roles.json"
    creates_file.write_text(json.dumps([
        {"company": "New Co", "title": "Head of Design", "url": "https://example.com/job/1"}
    ]))

    import sys
    old_argv = sys.argv

    sys.argv = ["gmail_sweep_apply.py", str(creates_file), "--create"]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out

    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM roles WHERE company = 'New Co'").fetchone()[0]
    conn.close()
    assert n == 0, "dry run must not have created anything"

    sys.argv = ["gmail_sweep_apply.py", str(creates_file), "--create", "--apply"]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "Created 1 role" in out


def test_create_without_apply_never_issues_a_write_statement(tmp_path, monkeypatch):
    """The whole staged-review workflow leans on --create alone being
    strictly read-only — a row count check (as in the CLI test above)
    proves nothing got created, but not that nothing was ever attempted.
    This intercepts every SQL statement any connection executes during a
    --create-without---apply run and fails immediately if any of them is
    an INSERT/UPDATE/DELETE/REPLACE, structurally proving the guarantee
    rather than trusting the current code path never to regress."""
    creates_file = tmp_path / "new_roles.json"
    creates_file.write_text(json.dumps([
        {"company": "New Co", "title": "Head of Design", "url": "https://example.com/job/1"}
    ]))

    real_connect = dbmod.connect
    monkeypatch.setattr(gsa.dbmod, "connect", lambda *a, **kw: _WriteGuardedConn(real_connect(*a, **kw)))

    import sys
    old_argv = sys.argv
    sys.argv = ["gmail_sweep_apply.py", str(creates_file), "--create"]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0

    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM roles WHERE company = 'New Co'").fetchone()[0]
    conn.close()
    assert n == 0


def test_create_without_apply_does_not_write_even_with_force(tmp_path, capsys):
    # --create alone (no --apply) must still be a pure preview, same as the
    # default update mode — "only functions together with --apply".
    _insert_role("Acme Corp", "Head of Design", status="rejected")
    creates_file = tmp_path / "new_roles.json"
    creates_file.write_text(json.dumps([
        {"company": "Acme Corp", "title": "Head of Design", "url": "https://example.com/job/2"}
    ]))

    import sys
    old_argv = sys.argv
    sys.argv = ["gmail_sweep_apply.py", str(creates_file), "--create", "--force"]
    try:
        rc = gsa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out

    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM roles WHERE company = 'Acme Corp'").fetchone()[0]
    conn.close()
    assert n == 1, "still just the original row — force without --apply must not create"
