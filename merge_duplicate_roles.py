#!/usr/bin/env python3
"""
Merge duplicate role records (repeatable, not a one-off).
===========================================================
Same requisition, two rows: one from the tracker.md import (source='manual',
curated status/notes/applied_date, no url) and one the scanner found
independently later (source='auto', has url/job_id/portal). This recurs
every time the scanner discovers a job already tracked by hand before
the cockpit existed — job_id differs by construction (a synthetic "trk-..."
slug on the tracker row vs. the portal's own id on the scanner row), so
scan_into_db()'s own dedup (which matches on job_id) never catches it, and
the company+title repost_of lookup only fires for ignored/rejected chains,
not an ordinary submitted row.

Detection: same company (normalized) + same requisition number, extracted
from the tracker row's title (a trailing "(NNNNN)" or "(req NNNNN)" — the
convention the original tracker.md notes already used) and from the
scanner row's job_id/url (a trailing "_NNNNN"). Both must resolve to the
same digits and the same company.

Merge direction is fixed and asymmetric: the TRACKER row (curated status,
notes, applied_date — the record you actually look at) absorbs the
SCANNER row's url/job_id/portal, plus any notes on the scanner row
(appended, not overwritten) and the EARLIER of the two first_seen values
(the scanner's first_seen is genuine information about when the posting
went live that a tracker-imported row never had). The scanner row is
never deleted — it's stamped merged_into = <tracker row id> and must be
excluded from every active count/filter/view that checks for it, the
same convention repost_of already established for lineage.

job_id is UNIQUE, so moving it requires clearing the scanner row's job_id
first (NULLs don't collide under SQLite's UNIQUE) before writing it onto
the tracker row. This is deliberate, not incidental: scan_into_db()'s own
"already known" check (`if jid in existing_ids`) reads job_id off ALL
rows regardless of merged_into, so after the move a future scan of the
same posting correctly bumps the TRACKER row's last_seen — the canonical
record — instead of either re-touching the now-inert scanner row or,
worse, inserting a THIRD duplicate.

SAFETY: default mode is --preview (writes nothing). Only --apply mutates,
and backs up first.

Usage:
    python3 merge_duplicate_roles.py            # preview only, no writes
    python3 merge_duplicate_roles.py --apply     # perform the merges
"""

import re
import sys

import db as dbmod

REQ_ID_RE_TITLE = re.compile(r"\(\s*(?:req\s+)?(\d{5,})\s*\)\s*$", re.IGNORECASE)
REQ_ID_RE_URL = re.compile(r"_(\d{5,})(?:[/?#-]|$)")


def _title_req_id(title):
    m = REQ_ID_RE_TITLE.search(title or "")
    return m.group(1) if m else None


def _url_req_id(*texts):
    for t in texts:
        m = REQ_ID_RE_URL.search(t or "")
        if m:
            return m.group(1)
    return None


def _norm_company(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def find_candidate_pairs(conn):
    """Every (tracker_row, scanner_row) dict pair that looks like the same
    requisition. Only considers rows not already merged either direction."""
    roles = [dict(r) for r in conn.execute(
        "SELECT * FROM roles WHERE merged_into IS NULL"
    )]

    tracker_rows = []
    for r in roles:
        if r["url"]:
            continue
        rid = _title_req_id(r["title"])
        if rid:
            tracker_rows.append((rid, r))

    scanner_rows = []
    for r in roles:
        if not r["url"]:
            continue
        rid = _url_req_id(r["job_id"], r["url"])
        if rid:
            scanner_rows.append((rid, r))

    pairs = []
    for t_rid, t_role in tracker_rows:
        for s_rid, s_role in scanner_rows:
            if t_rid == s_rid and _norm_company(t_role["company"]) == _norm_company(s_role["company"]):
                pairs.append((t_role, s_role))
    return pairs


def format_preview(pairs):
    if not pairs:
        return "No candidate duplicate pairs found."
    lines = [f"Found {len(pairs)} candidate pair(s) — nothing written yet.\n"]
    for t, s in pairs:
        lines.append(f"TRACKER row id {t['id']}: {t['company']} / {t['title']!r}")
        lines.append(f"    status={t['status']}  applied_date={t['applied_date'] or '—'}  "
                      f"first_seen={t['first_seen'] or '—'}  job_id={t['job_id']}  "
                      f"url={t['url'] or '(none)'}")
        lines.append(f"    notes={t['notes'] or '(none)'}")
        lines.append(f"SCANNER row id {s['id']}: {s['company']} / {s['title']!r}")
        lines.append(f"    status={s['status']}  first_seen={s['first_seen'] or '—'}  "
                      f"job_id={s['job_id']}  portal={s['portal']}  url={s['url']}")
        lines.append(f"    notes={s['notes'] or '(none)'}")
        seen_values = [v for v in (t["first_seen"], s["first_seen"]) if v]
        earlier_seen = min(seen_values) if seen_values else t["first_seen"]
        lines.append(f"  -> tracker row {t['id']} absorbs url/job_id/portal from scanner row {s['id']}, "
                      f"appends scanner's notes (if any), first_seen -> {earlier_seen or '—'}")
        lines.append(f"  -> scanner row {s['id']} gets merged_into={t['id']}, "
                      "job_id cleared, never deleted, excluded from active views")
        lines.append("")
    return "\n".join(lines)


def apply_merge(conn, pairs):
    dbmod.backup_db("merge-duplicate-roles")
    ts = dbmod.now()
    merged = 0
    for t, s in pairs:
        # Clear the scanner row's job_id FIRST — job_id is UNIQUE, and NULLs
        # don't collide, so this frees the value before the tracker row
        # claims it.
        conn.execute(
            "UPDATE roles SET job_id = NULL, merged_into = ?, updated_at = ? WHERE id = ?",
            (t["id"], ts, s["id"]),
        )

        # Notes: append rather than discard — the scanner row rarely carries
        # any, but when it does (or ever does in the future), losing them
        # silently would defeat "never unrecoverable without going to look."
        merged_notes = t["notes"] or ""
        if s["notes"] and s["notes"] not in merged_notes:
            merged_notes = (merged_notes + f"\n\n[merged from row {s['id']}] {s['notes']}").strip()

        # first_seen: the earlier of the two. ISO 'YYYY-MM-DD' strings sort
        # lexicographically, so a plain min() is correct — the scanner's
        # first_seen is real information about when the posting went live
        # that the tracker-imported row never had.
        seen_values = [v for v in (t["first_seen"], s["first_seen"]) if v]
        merged_first_seen = min(seen_values) if seen_values else t["first_seen"]

        conn.execute(
            "UPDATE roles SET url = ?, job_id = ?, portal = ?, notes = ?, "
            "first_seen = ?, updated_at = ? WHERE id = ?",
            (s["url"], s["job_id"], s["portal"], merged_notes or None,
             merged_first_seen, ts, t["id"]),
        )
        merged += 1
    conn.commit()
    return merged


def main():
    conn = dbmod.connect()
    pairs = find_candidate_pairs(conn)

    if "--apply" in sys.argv:
        merged = apply_merge(conn, pairs)
        conn.close()
        print(f"Applied: {merged} pair(s) merged.")
        return

    print(format_preview(pairs))
    conn.close()
    print("PREVIEW ONLY — nothing written. Re-run with --apply to perform the merge.")


if __name__ == "__main__":
    main()
