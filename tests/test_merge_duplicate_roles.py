import app
import db as dbmod
import merge_duplicate_roles as mdr


def _insert(company, title, url=None, job_id=None, portal=None, status="sourced",
            source="manual", applied_date=None, notes=None, first_seen=None):
    conn = dbmod.connect()
    ts = dbmod.now()
    if job_id is None:
        job_id = f"job-{company}-{title}".lower().replace(" ", "-")
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, url, portal, status, "
        "applied_date, notes, jd_source, first_seen, last_seen, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, source, company, title, "Review", url, portal, status,
         applied_date, notes, "none", first_seen or dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _make_pair(scanner_notes=None):
    tracker_id = _insert(
        "Logitech", "Sr. UX Designer, Logitech G (req 145142)",
        job_id="trk-logitech-sr-ux-145142", status="submitted",
        applied_date="2026-06-01", notes="Applied via referral.",
        first_seen="2026-06-01",
    )
    scanner_id = _insert(
        "Logitech", "Sr. User Experience Designer",
        url="https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142",
        job_id="Sr-User-Experience-Designer_145142", portal="workday", source="auto",
        notes=scanner_notes, first_seen="2026-02-10",
    )
    return tracker_id, scanner_id


def test_title_req_id_extraction():
    assert mdr._title_req_id("Senior Hardware Product UX Designer (146581)") == "146581"
    assert mdr._title_req_id("Sr. UX Designer, Logitech G (req 145142)") == "145142"
    assert mdr._title_req_id("Head of Design") is None
    assert mdr._title_req_id("Lead UI/UX Designer") is None


def test_url_req_id_extraction():
    assert mdr._url_req_id("Sr-User-Experience-Designer_145142") == "145142"
    assert mdr._url_req_id(
        "https://logitech.wd5.myworkdayjobs.com/.../Sr-User-Experience-Designer_145142"
    ) == "145142"
    assert mdr._url_req_id("User-Experience-Designer_202607-118105-1") == "202607"
    assert mdr._url_req_id("no-numbers-here") is None


def test_find_candidate_pairs_matches_same_company_and_req_id():
    tracker_id, scanner_id = _make_pair()
    # A distractor: different company, same req-id shape — must not match.
    _insert("Roche", "Lead UI/UX Designer",
            url="https://roche.wd3.myworkdayjobs.com/en-US/roche-ext/job/Basel/User-Experience-Designer_202607-118105-1",
            job_id="User-Experience-Designer_202607-118105-1", portal="workday", source="auto")

    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    conn.close()

    ids = [(t["id"], s["id"]) for t, s in pairs]
    assert (tracker_id, scanner_id) in ids
    assert len(pairs) == 1


def test_find_candidate_pairs_ignores_already_merged_rows():
    tracker_id, scanner_id = _make_pair()
    conn = dbmod.connect()
    conn.execute("UPDATE roles SET merged_into = ? WHERE id = ?", (tracker_id, scanner_id))
    conn.commit()
    pairs = mdr.find_candidate_pairs(conn)
    conn.close()
    assert pairs == []


def test_apply_merge_folds_fields_and_never_deletes():
    tracker_id, scanner_id = _make_pair()
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    merged = mdr.apply_merge(conn, pairs)
    assert merged == 1

    tracker_row = conn.execute("SELECT * FROM roles WHERE id = ?", (tracker_id,)).fetchone()
    scanner_row = conn.execute("SELECT * FROM roles WHERE id = ?", (scanner_id,)).fetchone()

    # Tracker row absorbs url/job_id/portal, keeps its own curated fields.
    assert tracker_row["url"] == "https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142"
    assert tracker_row["job_id"] == "Sr-User-Experience-Designer_145142"
    assert tracker_row["portal"] == "workday"
    assert tracker_row["status"] == "submitted"
    assert tracker_row["applied_date"] == "2026-06-01"
    assert tracker_row["notes"] == "Applied via referral."
    assert tracker_row["merged_into"] is None
    # first_seen: the earlier of the two (scanner's 2026-02-10 predates the
    # tracker row's 2026-06-01) — real information about when the posting
    # actually went live, not discarded just because the tracker row didn't have it.
    assert tracker_row["first_seen"] == "2026-02-10"

    # Scanner row: never deleted, job_id freed, stamped merged_into.
    assert scanner_row is not None
    assert scanner_row["merged_into"] == tracker_id
    assert scanner_row["job_id"] is None
    conn.close()


def test_apply_merge_appends_scanner_notes_rather_than_discarding():
    tracker_id, scanner_id = _make_pair(scanner_notes="Auto-found via Workday CXS scan.")
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    mdr.apply_merge(conn, pairs)
    tracker_row = conn.execute("SELECT notes FROM roles WHERE id = ?", (tracker_id,)).fetchone()
    conn.close()
    assert "Applied via referral." in tracker_row["notes"]
    assert "Auto-found via Workday CXS scan." in tracker_row["notes"]


def test_apply_merge_with_no_scanner_notes_leaves_tracker_notes_unchanged():
    tracker_id, scanner_id = _make_pair(scanner_notes=None)
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    mdr.apply_merge(conn, pairs)
    tracker_row = conn.execute("SELECT notes FROM roles WHERE id = ?", (tracker_id,)).fetchone()
    conn.close()
    assert tracker_row["notes"] == "Applied via referral."


def test_apply_merge_frees_job_id_without_unique_collision():
    """job_id is UNIQUE — the scanner row's job_id must be cleared before the
    tracker row claims it, or this raises IntegrityError."""
    tracker_id, scanner_id = _make_pair()
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    mdr.apply_merge(conn, pairs)  # must not raise
    conn.close()


def test_merged_row_excluded_from_api_roles():
    tracker_id, scanner_id = _make_pair()
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    mdr.apply_merge(conn, pairs)
    conn.close()

    client = app.app.test_client()
    r = client.get("/api/roles")
    ids = [role["id"] for role in r.get_json()["roles"]]
    assert tracker_id in ids
    assert scanner_id not in ids


def test_preview_never_writes():
    tracker_id, scanner_id = _make_pair()
    conn = dbmod.connect()
    pairs = mdr.find_candidate_pairs(conn)
    text = mdr.format_preview(pairs)
    assert str(tracker_id) in text
    assert str(scanner_id) in text

    row = conn.execute("SELECT merged_into, job_id FROM roles WHERE id = ?", (scanner_id,)).fetchone()
    conn.close()
    assert row["merged_into"] is None
    assert row["job_id"] is not None
