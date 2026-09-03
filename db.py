#!/usr/bin/env python3
"""
SQLite layer for the application cockpit.
=========================================
Source of truth for roles and their generated documents. Replaces the CSV for
reads (the CSV stays as an export target and as the scanner's own CLI store).

The `roles` table is the CSV schema (spec section 4) widened to hold the
tracker.md pipeline fields (interest / channel / next_action / applied_date /
due), so the cockpit can absorb the existing pipeline workflow instead of
sitting beside it. Those columns are nullable and stay empty until the
tracker.md reconciliation (Phase 0.5) fills them.

Unified status vocabulary merges the CSV and tracker.md sets:
    sourced | reviewing | drafted | submitted | interview | offer
    | rejected | ignored | expired
"""

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "cockpit.db"
BACKUP_DIR = Path(__file__).parent / "backups"
BACKUP_RETAIN = 20
SCAN_RUN_RETAIN = 20
# A company must error on this many consecutive most-recent runs before the
# UI flags it "failing since <date>" — one transient network blip shouldn't
# trip it, but it shouldn't take weeks to notice a rotted scraper either.
CONSECUTIVE_FAILURE_THRESHOLD = 3

# Unified status vocabulary (CSV set + tracker.md set, deduped).
STATUSES = [
    "sourced", "reviewing", "drafted", "submitted",
    "interview", "offer", "rejected", "ignored", "expired",
]

# CSV status -> unified status. Anything unmapped passes through unchanged if it
# is already a valid unified status, else falls back to 'sourced'.
CSV_STATUS_MAP = {
    "new": "sourced",
    "applied": "submitted",
    "ignored": "ignored",
    "expired": "expired",
    "reviewing": "reviewing",
}

CATEGORIES = ["Leadership", "Advisory", "Senior IC", "Review"]

# Controlled vocabulary for roles.interest (Phase 3c). Undefined is not a
# stored string — it's the absence of one: NULL in the column, "" from the
# client, displayed as "Undefined". Migrated from the free-text values this
# column held before (REAL, REAL?, QUOTA?): see HANDOFF.md for the mapping.
INTERESTS = ["Real", "Quota"]

# Controlled vocabulary for roles.decision_reason — why NIKO passed on a role
# (status='ignored' only; see the column comment in SCHEMA and the
# enforcement in api_update for why 'rejected' rows must never carry one).
DECISION_REASONS = ["language", "location", "seniority", "domain", "comp", "other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id            INTEGER PRIMARY KEY,
    job_id        TEXT UNIQUE,
    source        TEXT DEFAULT 'auto',        -- auto | manual
    company       TEXT,
    title         TEXT,
    category      TEXT,                        -- Leadership | Advisory | Senior IC | Review
    score         REAL,
    location      TEXT,
    url           TEXT,
    portal        TEXT,
    posted        TEXT,
    status        TEXT DEFAULT 'sourced',
    -- tracker.md pipeline fields (nullable; filled by the Phase 0.5 reconcile)
    interest      TEXT,                        -- REAL | QUOTA | ? ...
    channel       TEXT,
    next_action   TEXT,
    applied_date  TEXT,
    due           TEXT,
    notes         TEXT,                        -- free-text triage (old CSV 'reason')
    jd_text       TEXT,
    jd_source     TEXT DEFAULT 'none',         -- fetch | browser | paste | none
    first_seen    TEXT,
    last_seen     TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    -- Decision memory: why NIKO passed. Meaningful ONLY when status='ignored'
    -- — a role the employer rejected already has that fact in status itself
    -- (status='rejected'), so decision_reason there would mix "why I passed"
    -- with "why they passed" and stop being a clean filter. Enforced in
    -- api_update: a write that sets decision_reason is rejected unless the
    -- role's status is 'ignored'.
    decision_reason TEXT,                      -- language|location|seniority|domain|comp|other
    decision_note   TEXT,                      -- free text
    -- Repost lineage: points at the EARLIEST row in the chain, not the
    -- immediate predecessor, so any row is one hop from its origin.
    repost_of       INTEGER REFERENCES roles(id),
    -- Duplicate-record merge (not repost lineage — same requisition, two
    -- SOURCES: one row from the tracker.md import, one the scanner found
    -- independently via its own url/job_id/portal). Set on the row that
    -- got folded IN; that row is never deleted, only excluded from active
    -- counts/filters/views everywhere merged_into IS NOT NULL is checked.
    merged_into     INTEGER REFERENCES roles(id)
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    doc_type      TEXT,                        -- cv | cover_letter
    version       INTEGER,
    format        TEXT,                        -- md | txt | pdf
    file_path     TEXT,
    content_md    TEXT,
    content_json  TEXT,                        -- structured CV content (Phase 3a); null for cover letters
    model         TEXT,
    critic_notes  TEXT,                        -- JSON: source-fidelity flags + [GAP: ...] markers (Phase 3a)
    status        TEXT DEFAULT 'draft',        -- draft | approved
    -- Set when this version came from hand-editing an earlier one, to that
    -- earlier version's id; NULL means a model produced it. One column
    -- gives both the boolean ("did I write this") and the lineage ("from
    -- what"), the same way roles.repost_of and roles.merged_into do.
    -- model is NULL on an edited row: that column records which model
    -- produced the text, and for a hand-edit the honest answer is none.
    edited_from   INTEGER REFERENCES documents(id),
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_role ON documents(role_id);

-- Scan observability (append-only, never updated after insert). Two
-- questions the UI must answer: did the scan work, and why did nothing
-- land. dropped_titles/dropped_locations on scan_run_companies are what
-- answer the second one — the actual titles/locations that fell out of
-- the funnel, not just a count, capped at 50 per company per run.
CREATE TABLE IF NOT EXISTS scan_runs (
    id                      INTEGER PRIMARY KEY,
    started_at              TEXT,
    finished_at             TEXT,
    trigger                 TEXT,               -- manual | scheduled
    companies_total         INTEGER,
    companies_ok            INTEGER,
    companies_error         INTEGER,
    raw_total               INTEGER,
    title_matched_total     INTEGER,
    location_matched_total  INTEGER,
    inserted_total          INTEGER,
    expired_total           INTEGER
);

CREATE TABLE IF NOT EXISTS scan_run_companies (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    company            TEXT,
    portal             TEXT,
    outcome            TEXT,               -- ok | error | skipped_robots | skipped_disabled
    error_detail       TEXT,               -- HTTP status or exception, verbatim
    raw_count          INTEGER,
    title_matched      INTEGER,
    location_matched   INTEGER,
    inserted           INTEGER,
    dropped_titles     TEXT,               -- JSON array, capped at 50
    dropped_locations  TEXT                -- JSON array, capped at 50
);

CREATE INDEX IF NOT EXISTS idx_scan_run_companies_run ON scan_run_companies(run_id);
"""

# Columns a client is allowed to edit inline via /api/roles/<id>/update.
EDITABLE_FIELDS = {
    "status", "category", "notes", "interest",
    "channel", "next_action", "applied_date", "due", "jd_text",
    "decision_reason", "decision_note",
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def connect(db_path=None):
    """db_path defaults to the CURRENT value of module-level DB_PATH, resolved
    at call time — not bound at import. A default of `db_path=DB_PATH` looks
    equivalent but isn't: Python evaluates a default argument expression once,
    when the function is defined, so reassigning `db.DB_PATH` later (e.g. to
    redirect tests at a temp file) would silently have no effect on this
    function. That was a real bug — see tests/conftest.py."""
    conn = sqlite3.connect(db_path if db_path is not None else DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_db(reason, db_path=None):
    """Snapshot cockpit.db to backups/cockpit-YYYYMMDD-HHMMSS-<reason>.db before
    a bulk mutation, then prune to the most recent BACKUP_RETAIN snapshots.

    cockpit.db is gitignored, so this is the only recovery path for data (the
    phase tags protect code only). Call this at the top of every code path
    that mutates roles in bulk — the tracker reconcile, a controlled-
    vocabulary migration, scan_into_db, gmail_sweep_apply, merge_duplicate_roles
    — before any write. Same call-time-resolution rule as connect(): db_path
    is read from the current module-level DB_PATH here, not bound as a
    default argument.

    No-ops (returns None) if there's no existing database file to copy yet.
    """
    src = Path(db_path) if db_path is not None else DB_PATH
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "-", reason).strip("-") or "unlabeled"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"cockpit-{ts}-{safe_reason}.db"
    shutil.copy2(src, dest)
    print(f"[backup_db] snapshot written: {dest}")
    _prune_backups()
    return dest


def record_scan_run(conn, trigger, started_at, finished_at, company_results,
                     inserted_total, expired_total):
    """Persist one scan run + its per-company detail — append-only, never
    updated after insert. company_results is a list of dicts:
    {company, portal, outcome, error_detail, raw_count, title_matches,
    dropped_titles, location_matches, dropped_locations, inserted}, where
    title_matches/location_matches are full match-dict lists (only their
    length is stored) and dropped_titles/dropped_locations are the actual
    dropped values, capped at 50 each — that cap is what keeps "why did
    nothing land" answerable without the table growing unbounded on a
    company that drops hundreds of titles every run.

    Prunes to the newest SCAN_RUN_RETAIN runs afterward, same pattern as
    backup_db's file retention."""
    companies_total = len(company_results)
    companies_ok = sum(1 for c in company_results if c["outcome"] == "ok")
    companies_error = sum(1 for c in company_results if c["outcome"] == "error")
    raw_total = sum(c["raw_count"] for c in company_results)
    title_matched_total = sum(len(c["title_matches"]) for c in company_results)
    location_matched_total = sum(len(c["location_matches"]) for c in company_results)

    cur = conn.execute(
        """INSERT INTO scan_runs (
            started_at, finished_at, trigger, companies_total, companies_ok,
            companies_error, raw_total, title_matched_total,
            location_matched_total, inserted_total, expired_total
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (started_at, finished_at, trigger, companies_total, companies_ok,
         companies_error, raw_total, title_matched_total,
         location_matched_total, inserted_total, expired_total),
    )
    run_id = cur.lastrowid
    for c in company_results:
        conn.execute(
            """INSERT INTO scan_run_companies (
                run_id, company, portal, outcome, error_detail, raw_count,
                title_matched, location_matched, inserted, dropped_titles,
                dropped_locations
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, c["company"], c["portal"], c["outcome"], c.get("error_detail"),
             c["raw_count"], len(c["title_matches"]), len(c["location_matches"]),
             c.get("inserted", 0), json.dumps(c["dropped_titles"][:50]),
             json.dumps(c["dropped_locations"][:50])),
        )
    conn.commit()
    _prune_scan_runs(conn)
    return run_id


def _prune_scan_runs(conn):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM scan_runs ORDER BY started_at DESC, id DESC"
    )]
    stale = ids[SCAN_RUN_RETAIN:]
    if stale:
        conn.execute(f"DELETE FROM scan_runs WHERE id IN ({','.join('?' * len(stale))})", stale)
        conn.commit()


def failing_since(conn, company, threshold=CONSECUTIVE_FAILURE_THRESHOLD):
    """None unless `company` has outcome='error' on `threshold`+ consecutive
    MOST RECENT runs — then the started_at of the earliest run in that
    unbroken streak. A single transient failure never trips this; a
    scraper that's been dead for weeks does, and says since when."""
    rows = conn.execute(
        """SELECT sr.started_at, src.outcome FROM scan_run_companies src
           JOIN scan_runs sr ON sr.id = src.run_id
           WHERE src.company = ? ORDER BY sr.started_at DESC, sr.id DESC""",
        (company,),
    ).fetchall()
    streak_start = None
    count = 0
    for r in rows:
        if r["outcome"] != "error":
            break
        streak_start = r["started_at"]
        count += 1
    return streak_start if count >= threshold else None


def _prune_backups():
    snapshots = sorted(BACKUP_DIR.glob("cockpit-*.db"), key=lambda p: p.stat().st_mtime)
    for old in snapshots[:-BACKUP_RETAIN]:
        old.unlink()


def init_db(db_path=None):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    for col, coltype in (("content_json", "TEXT"),
                         ("edited_from", "INTEGER REFERENCES documents(id)")):
        if col not in doc_cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {coltype}")
    role_cols = {r["name"] for r in conn.execute("PRAGMA table_info(roles)")}
    for col, coltype in (("decision_reason", "TEXT"), ("decision_note", "TEXT"),
                         ("repost_of", "INTEGER REFERENCES roles(id)"),
                         ("merged_into", "INTEGER REFERENCES roles(id)")):
        if col not in role_cols:
            conn.execute(f"ALTER TABLE roles ADD COLUMN {col} {coltype}")
    conn.commit()
    return conn


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {DB_PATH}")
