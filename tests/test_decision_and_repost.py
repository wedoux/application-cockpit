"""
Decision memory (decision_reason/decision_note) and repost lineage
(repost_of), closing out the tracker.md drift work: a declined role
carries why, and a role the scanner re-surfaces gets linked to what was
already decided, instead of asking you to re-decide the same role every
scan.
"""
import app
import db as dbmod


_next_job_id = [0]


def _detailed_ok(matches):
    """One 'ok' scan_all_companies_detailed() company result whose
    location_matches equals title_matches — the old tests forced this via
    monkeypatch.setattr(app, "location_ok", lambda loc: True); now that
    location filtering happens inside job_scanner and scan_into_db just
    reads the precomputed location_matches, the fixture states the same
    intent directly instead of mocking a function that no longer exists
    in this code path."""
    return [{
        "company": matches[0]["company"], "portal": matches[0]["portal"],
        "outcome": "ok", "error_detail": None, "raw_count": len(matches),
        "title_matches": matches, "dropped_titles": [],
        "location_matches": matches, "dropped_locations": [],
    }]


def _insert_role(company, title, status="sourced", repost_of=None,
                  decision_reason=None, decision_note=None):
    _next_job_id[0] += 1
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, "
        "decision_reason, decision_note, repost_of, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}-{_next_job_id[0]}", "manual", company, title, "Leadership", status,
         decision_reason, decision_note, repost_of,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------

def test_schema_has_decision_and_repost_columns():
    conn = dbmod.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(roles)")}
    conn.close()
    assert {"decision_reason", "decision_note", "repost_of"} <= cols


def test_decision_fields_are_editable():
    assert {"decision_reason", "decision_note"} <= dbmod.EDITABLE_FIELDS


def test_decision_reasons_vocabulary():
    assert dbmod.DECISION_REASONS == ["language", "location", "seniority", "domain", "comp", "other"]


# ------------------------------------------------------------------
# Amendment A: decision_reason only meaningful on status='ignored'
# ------------------------------------------------------------------

def test_decision_reason_rejected_on_a_rejected_role():
    role_id = _insert_role("Acme", "Head of Design", status="rejected")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_reason", "value": "domain"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "decision_reason_wrong_status"

    conn = dbmod.connect()
    row = conn.execute("SELECT decision_reason FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["decision_reason"] is None, "the rejected write must not have landed"


def test_decision_reason_rejected_on_a_sourced_role():
    role_id = _insert_role("Acme", "Head of Design", status="sourced")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_reason", "value": "domain"})
    assert r.status_code == 400


def test_decision_reason_accepted_on_an_ignored_role():
    role_id = _insert_role("Acme", "Head of Design", status="ignored")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_reason", "value": "language"})
    assert r.status_code == 200

    conn = dbmod.connect()
    row = conn.execute("SELECT decision_reason FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["decision_reason"] == "language"


def test_decision_reason_rejects_values_outside_the_enum():
    role_id = _insert_role("Acme", "Head of Design", status="ignored")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_reason", "value": "not-a-real-reason"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad decision_reason"


def test_decision_reason_can_be_cleared_regardless_of_status():
    # Clearing (empty value -> NULL) removes the field, it doesn't set it —
    # must never be blocked by the ignored-only rule.
    role_id = _insert_role("Acme", "Head of Design", status="rejected",
                            decision_reason=None)
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_reason", "value": ""})
    assert r.status_code == 200


def test_decision_note_has_no_status_restriction():
    # Only decision_reason carries the semantic Amendment A is about — notes
    # are free text and were never asked to be gated the same way.
    role_id = _insert_role("Acme", "Head of Design", status="submitted")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/update", json={"field": "decision_note", "value": "just a note"})
    assert r.status_code == 200


# ------------------------------------------------------------------
# Scanner repost detection (scan_into_db, via app.scan_all_companies mocked)
# ------------------------------------------------------------------

def test_scan_links_a_new_posting_to_a_rejected_roles_chain_root(monkeypatch):
    original_id = _insert_role("Acme", "Senior Product Designer", status="rejected")

    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: _detailed_ok(
        [{"job_id": "new-req-999", "company": "Acme", "title": "Senior Product Designer",
          "location": "Lausanne", "url": "https://x", "portal": "test", "posted": "2026-08-20"}]
    ))

    app.scan_into_db()

    conn = dbmod.connect()
    row = conn.execute("SELECT repost_of FROM roles WHERE job_id = 'new-req-999'").fetchone()
    conn.close()
    assert row["repost_of"] == original_id


def test_scan_points_a_repost_at_the_chain_root_not_the_intermediate(monkeypatch):
    """Amendment B: repost_of always points at the EARLIEST row, so lineage
    is one hop from any row, even when the scanner discovers a THIRD
    generation of the same role."""
    root_id = _insert_role("Acme", "Senior Product Designer", status="rejected")
    gen2_id = _insert_role("Acme", "Senior Product Designer", status="ignored", repost_of=root_id)

    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: _detailed_ok(
        [{"job_id": "new-req-gen3", "company": "Acme", "title": "Senior Product Designer",
          "location": "Lausanne", "url": "https://x", "portal": "test", "posted": "2026-08-20"}]
    ))

    app.scan_into_db()

    conn = dbmod.connect()
    row = conn.execute("SELECT repost_of FROM roles WHERE job_id = 'new-req-gen3'").fetchone()
    conn.close()
    assert row["repost_of"] == root_id
    assert row["repost_of"] != gen2_id


def test_scan_leaves_repost_of_null_for_a_genuinely_new_role(monkeypatch):
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: _detailed_ok(
        [{"job_id": "brand-new-req", "company": "Globex", "title": "Product Designer",
          "location": "Lausanne", "url": "https://x", "portal": "test", "posted": "2026-08-20"}]
    ))

    app.scan_into_db()

    conn = dbmod.connect()
    row = conn.execute("SELECT repost_of FROM roles WHERE job_id = 'brand-new-req'").fetchone()
    conn.close()
    assert row["repost_of"] is None
