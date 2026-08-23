"""
Scan observability (db.py's scan_runs/scan_run_companies, app.py's
scan_into_db integration, and the /api/scan_runs/latest route).

Two questions the UI must answer: did the scan work, and why did nothing
land. dropped_titles/dropped_locations are what answer the second one.
"""
import json

import app
import db as dbmod


def _company_result(company="Acme", portal="greenhouse", outcome="ok", error_detail=None,
                     raw_count=0, title_matches=None, dropped_titles=None,
                     location_matches=None, dropped_locations=None, inserted=0):
    return {
        "company": company, "portal": portal, "outcome": outcome,
        "error_detail": error_detail, "raw_count": raw_count,
        "title_matches": title_matches or [], "dropped_titles": dropped_titles or [],
        "location_matches": location_matches or [], "dropped_locations": dropped_locations or [],
        "inserted": inserted,
    }


# ------------------------------------------------------------------
# db.record_scan_run / db.failing_since / retention
# ------------------------------------------------------------------

def test_record_scan_run_reconciles_the_funnel_totals():
    match = {"title": "Head of Design", "location": "Geneva"}
    results = [
        _company_result("Acme", raw_count=5, title_matches=[match, match], dropped_titles=["Warehouse Associate"] * 3,
                         location_matches=[match], dropped_locations=["Paris"], inserted=1),
        _company_result("Globex", outcome="error", error_detail="HTTP 500"),
    ]
    conn = dbmod.connect()
    run_id = dbmod.record_scan_run(conn, "manual", "2026-08-19 10:00:00", "2026-08-19 10:01:00",
                                    results, inserted_total=1, expired_total=0)
    run = conn.execute("SELECT * FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()

    assert run["companies_total"] == 2
    assert run["companies_ok"] == 1
    assert run["companies_error"] == 1
    assert run["raw_total"] == 5
    assert run["title_matched_total"] == 2
    assert run["location_matched_total"] == 1
    assert run["inserted_total"] == 1
    # The core funnel invariant: raw >= title >= location >= inserted.
    assert run["raw_total"] >= run["title_matched_total"] >= run["location_matched_total"] >= run["inserted_total"]


def test_record_scan_run_stores_dropped_titles_and_locations_verbatim():
    results = [_company_result(
        "Acme", raw_count=3,
        title_matches=[{"title": "Head of Design", "location": "Geneva"}],
        dropped_titles=["Warehouse Associate", "Responsable de l'Expérience Client"],
        location_matches=[], dropped_locations=["Geneva"],
    )]
    conn = dbmod.connect()
    run_id = dbmod.record_scan_run(conn, "manual", "2026-08-19 10:00:00", "2026-08-19 10:01:00",
                                    results, inserted_total=0, expired_total=0)
    row = conn.execute(
        "SELECT dropped_titles, dropped_locations FROM scan_run_companies WHERE run_id = ?", (run_id,)
    ).fetchone()
    conn.close()
    assert json.loads(row["dropped_titles"]) == ["Warehouse Associate", "Responsable de l'Expérience Client"]
    assert json.loads(row["dropped_locations"]) == ["Geneva"]


def test_record_scan_run_caps_dropped_titles_at_50_without_failing():
    results = [_company_result("Acme", raw_count=60, dropped_titles=[f"Title {i}" for i in range(60)])]
    conn = dbmod.connect()
    run_id = dbmod.record_scan_run(conn, "manual", "2026-08-19 10:00:00", "2026-08-19 10:01:00",
                                    results, inserted_total=0, expired_total=0)
    row = conn.execute("SELECT dropped_titles FROM scan_run_companies WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    assert len(json.loads(row["dropped_titles"])) == 50


def test_a_robots_blocked_provider_never_counts_as_companies_error():
    results = [_company_result("Blocked Co", outcome="skipped_robots", error_detail="disallowed by robots.txt")]
    conn = dbmod.connect()
    run_id = dbmod.record_scan_run(conn, "manual", "2026-08-19 10:00:00", "2026-08-19 10:01:00",
                                    results, inserted_total=0, expired_total=0)
    run = conn.execute("SELECT companies_error FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    company_row = conn.execute("SELECT outcome FROM scan_run_companies WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    assert run["companies_error"] == 0
    assert company_row["outcome"] == "skipped_robots"
    assert company_row["outcome"] != "error"


def test_scan_run_retention_prunes_to_the_newest_20(monkeypatch):
    monkeypatch.setattr(dbmod, "SCAN_RUN_RETAIN", 20)
    conn = dbmod.connect()
    for i in range(25):
        dbmod.record_scan_run(conn, "manual", f"2026-08-{i+1:02d} 10:00:00", f"2026-08-{i+1:02d} 10:01:00",
                               [_company_result("Acme")], inserted_total=0, expired_total=0)
    count = conn.execute("SELECT COUNT(*) c FROM scan_runs").fetchone()["c"]
    newest = conn.execute("SELECT started_at FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    oldest = conn.execute("SELECT started_at FROM scan_runs ORDER BY started_at ASC LIMIT 1").fetchone()
    conn.close()
    assert count == 20
    assert newest["started_at"] == "2026-08-25 10:00:00"
    assert oldest["started_at"] == "2026-08-06 10:00:00"  # the oldest 5 (01-05) got pruned


def test_scan_run_retention_cascades_company_rows():
    conn = dbmod.connect()
    run_id = dbmod.record_scan_run(conn, "manual", "2026-08-01 10:00:00", "2026-08-01 10:01:00",
                                    [_company_result("Acme")], inserted_total=0, expired_total=0)
    for i in range(2, 25):
        dbmod.record_scan_run(conn, "manual", f"2026-08-{i:02d} 10:00:00", f"2026-08-{i:02d} 10:01:00",
                               [_company_result("Acme")], inserted_total=0, expired_total=0)
    orphaned = conn.execute("SELECT COUNT(*) c FROM scan_run_companies WHERE run_id = ?", (run_id,)).fetchone()["c"]
    conn.close()
    assert orphaned == 0  # the pruned run's company rows must go with it, not linger


def test_failing_since_requires_the_threshold_consecutive_errors():
    conn = dbmod.connect()
    # Two errors then an ok, most recent last — not a qualifying streak
    # at threshold=3 even though errors exist in the history.
    dbmod.record_scan_run(conn, "manual", "2026-08-01 10:00:00", "x", [_company_result("Acme", outcome="error", error_detail="e")], 0, 0)
    dbmod.record_scan_run(conn, "manual", "2026-08-02 10:00:00", "x", [_company_result("Acme", outcome="error", error_detail="e")], 0, 0)
    dbmod.record_scan_run(conn, "manual", "2026-08-03 10:00:00", "x", [_company_result("Acme", outcome="ok")], 0, 0)
    assert dbmod.failing_since(conn, "Acme") is None

    dbmod.record_scan_run(conn, "manual", "2026-08-04 10:00:00", "x", [_company_result("Acme", outcome="error", error_detail="e")], 0, 0)
    dbmod.record_scan_run(conn, "manual", "2026-08-05 10:00:00", "x", [_company_result("Acme", outcome="error", error_detail="e")], 0, 0)
    assert dbmod.failing_since(conn, "Acme") is None  # only 2 consecutive so far

    dbmod.record_scan_run(conn, "manual", "2026-08-06 10:00:00", "x", [_company_result("Acme", outcome="error", error_detail="e")], 0, 0)
    since = dbmod.failing_since(conn, "Acme")  # 3rd consecutive — trips it
    conn.close()
    assert since == "2026-08-04 10:00:00"  # the earliest run in the unbroken streak


# ------------------------------------------------------------------
# app.scan_into_db() integration
# ------------------------------------------------------------------

def test_scan_into_db_records_a_run_with_reconciled_funnel(monkeypatch):
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: [{
        "company": "Acme", "portal": "greenhouse", "outcome": "ok", "error_detail": None,
        "raw_count": 3,
        "title_matches": [
            {"job_id": "a1", "company": "Acme", "title": "Head of Design", "location": "Geneva",
             "url": "https://x/1", "portal": "greenhouse", "posted": "2026-08-19"},
            {"job_id": "a2", "company": "Acme", "title": "Senior Designer", "location": "Paris",
             "url": "https://x/2", "portal": "greenhouse", "posted": "2026-08-19"},
        ],
        "dropped_titles": ["Warehouse Associate"],
        "location_matches": [
            {"job_id": "a1", "company": "Acme", "title": "Head of Design", "location": "Geneva",
             "url": "https://x/1", "portal": "greenhouse", "posted": "2026-08-19"},
        ],
        "dropped_locations": ["Paris"],
    }])

    app.scan_into_db()

    conn = dbmod.connect()
    run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    company_row = conn.execute(
        "SELECT * FROM scan_run_companies WHERE run_id = ?", (run["id"],)
    ).fetchone()
    role = conn.execute("SELECT * FROM roles WHERE job_id = 'a1'").fetchone()
    missing = conn.execute("SELECT * FROM roles WHERE job_id = 'a2'").fetchone()
    conn.close()

    assert run["raw_total"] == 3
    assert run["title_matched_total"] == 2
    assert run["location_matched_total"] == 1
    assert run["inserted_total"] == 1
    assert run["raw_total"] >= run["title_matched_total"] >= run["location_matched_total"] >= run["inserted_total"]
    assert company_row["inserted"] == 1
    assert json.loads(company_row["dropped_titles"]) == ["Warehouse Associate"]
    assert json.loads(company_row["dropped_locations"]) == ["Paris"]
    # Only the location-matched posting (a1) actually lands as a row —
    # a2 cleared the title filter but not location, so never inserted.
    assert role is not None
    assert missing is None


def test_scan_into_db_all_locations_flag_uses_title_matches_not_location_matches(monkeypatch):
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: [{
        "company": "Acme", "portal": "greenhouse", "outcome": "ok", "error_detail": None,
        "raw_count": 1,
        "title_matches": [{"job_id": "a2", "company": "Acme", "title": "Senior Designer",
                            "location": "Paris", "url": "https://x/2", "portal": "greenhouse",
                            "posted": "2026-08-19"}],
        "dropped_titles": [],
        "location_matches": [],  # Paris drops on location
        "dropped_locations": ["Paris"],
    }])

    app.scan_into_db(all_locations=True)

    conn = dbmod.connect()
    role = conn.execute("SELECT * FROM roles WHERE job_id = 'a2'").fetchone()
    conn.close()
    assert role is not None  # all_locations=True bypasses the location filter for insertion


def test_scan_into_db_a_clean_zero_match_company_is_ok_not_error(monkeypatch):
    """A real company's real first run against a live Oracle HCM provider:
    48 postings, 0 title matches, outcome='ok'. Regression: the old
    scanned_companies set was derived from raw_matches presence, so a
    clean zero-match company could never have its own stale rows
    auto-expired — fixed by deriving it from outcome=='ok'."""
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: [{
        "company": "Acme", "portal": "oracle_hcm", "outcome": "ok", "error_detail": None,
        "raw_count": 48, "title_matches": [], "dropped_titles": ["Security Engineer"] * 10,
        "location_matches": [], "dropped_locations": [],
    }])

    result = app.scan_into_db()

    conn = dbmod.connect()
    run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    company_row = conn.execute(
        "SELECT outcome FROM scan_run_companies WHERE run_id = ?", (run["id"],)
    ).fetchone()
    conn.close()
    assert company_row["outcome"] == "ok"
    assert run["companies_error"] == 0
    assert result["ok"] is True


# ------------------------------------------------------------------
# GET /api/scan_runs/latest
# ------------------------------------------------------------------

def test_api_scan_runs_latest_returns_none_before_any_scan():
    client = app.app.test_client()
    r = client.get("/api/scan_runs/latest")
    d = r.get_json()
    assert d["run"] is None
    assert d["companies"] == []


def test_api_scan_runs_latest_reflects_the_most_recent_run(monkeypatch):
    monkeypatch.setattr(app, "scan_all_companies_detailed", lambda: [{
        "company": "Acme", "portal": "greenhouse", "outcome": "error", "error_detail": "HTTP 500",
        "raw_count": 0, "title_matches": [], "dropped_titles": [],
        "location_matches": [], "dropped_locations": [],
    }])
    app.scan_into_db()

    client = app.app.test_client()
    r = client.get("/api/scan_runs/latest")
    d = r.get_json()
    assert d["run"]["companies_error"] == 1
    assert d["companies"][0]["company"] == "Acme"
    assert d["companies"][0]["outcome"] == "error"
    assert d["companies"][0]["dropped_titles"] == []


def test_api_scan_runs_latest_flags_failing_since_after_threshold_errors(monkeypatch):
    def erroring():
        return [{
            "company": "Acme", "portal": "greenhouse", "outcome": "error", "error_detail": "HTTP 500",
            "raw_count": 0, "title_matches": [], "dropped_titles": [],
            "location_matches": [], "dropped_locations": [],
        }]
    monkeypatch.setattr(app, "scan_all_companies_detailed", erroring)

    app.scan_into_db()
    app.scan_into_db()
    client = app.app.test_client()
    d = client.get("/api/scan_runs/latest").get_json()
    assert d["companies"][0]["failing_since"] is None  # only 2 consecutive so far

    app.scan_into_db()
    d = client.get("/api/scan_runs/latest").get_json()
    assert d["companies"][0]["failing_since"] is not None  # 3rd consecutive trips it
