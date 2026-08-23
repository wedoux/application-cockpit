#!/usr/bin/env python3
"""
Gmail sweep write path (single source of truth close-out).
=============================================================
The scheduled Gmail sweep discovers reality that only exists in email —
replies, rejections, confirmed applications — and used to write that
discovery into tracker.md, a markdown file the cockpit never read. This is
its replacement write path: the sweep proposes updates as JSON, this script
applies them to cockpit.db directly. tracker.md is now an archive (see
HANDOFF.md); this is the only write path going forward.

Usage:
    python3 gmail_sweep_apply.py updates.json                    # dry run (default): snapshot + diff, writes nothing
    python3 gmail_sweep_apply.py updates.json --apply             # also commits the diff

    python3 gmail_sweep_apply.py new_roles.json --create          # preview creates, writes nothing
    python3 gmail_sweep_apply.py new_roles.json --create --apply  # also commits the creates
    python3 gmail_sweep_apply.py new_roles.json --create --apply --force   # ...and force past duplicate matches

updates.json (default mode): a JSON list of objects:
    [{"company": "...", "title": "...", "status": "...", "note": "..."}, ...]
"status" must be one of db.STATUSES (omit to leave status untouched).
"note" is optional free text — APPENDED to the role's existing notes in
brackets, never overwritten, matching how every other status-changing note
in this codebase is recorded.

Matching is normalized company+title against every existing role — the same
rule app.py's manual-add duplicate detection uses (_norm / _find_duplicate),
so a company spelled or cased slightly differently in an email still
matches the intended row. Default mode NEVER creates a role: an update with
no match is reported under UNMATCHED and skipped.

new_roles.json (--create mode): a JSON list of genuinely new finds, staged
by the sweep rather than created unattended:
    [{"company": "...", "title": "...", "url": "...", "source_channel": "...",
      "source_email_date": "...", "suggested_category": "..."}, ...]
"company" and "url" are required; "title" and "suggested_category" are
best-effort from an email snippet, so they're optional. "source_channel" is
optional too (one of SOURCE_CHANNEL_LABELS below, e.g. "gmail" or
"careers_page") and drives the provenance note — the manual careers-page
sweep feeds this same --create path now, so the note can no longer assert
"Gmail sweep" unconditionally. Omitting it falls back to "Gmail sweep" only
when source_email_date is also present (a real inference, not a default —
an email date implies email); with neither, the note just says "sweep",
not a channel it can't back up. A row matching an EXISTING role on
normalized company+title is refused, not created — pass --force to create
it anyway (e.g. a deliberate re-application). --create only creates rows
together with --apply; without --apply it previews same as the default
mode. Rows land with status='sourced', source='sweep' (so scan_into_db's
auto-expire, which only ever touches source='auto' rows, leaves them
alone), and a note recording the source channel and, if given, the email date.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import app as app_module
import db as dbmod


def load_updates(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("updates file must be a JSON list of {company, title, status, note} objects")
    return data


def validate(update):
    problems = []
    if not update.get("company"):
        problems.append("missing company")
    if not update.get("title"):
        problems.append("missing title")
    status = update.get("status")
    if status is not None and status not in dbmod.STATUSES:
        problems.append(f"bad status: {status!r}")
    return problems


def plan(conn, updates):
    """Return (matched, unmatched, invalid). matched is a list of
    (update, role, changes) where changes is {field: (old, new)}."""
    matched, unmatched, invalid = [], [], []
    for u in updates:
        problems = validate(u)
        if problems:
            invalid.append((u, problems))
            continue
        dup = app_module._find_duplicate(conn, u.get("company", ""), u.get("title", ""))
        if not dup:
            unmatched.append(u)
            continue
        role = conn.execute("SELECT * FROM roles WHERE id = ?", (dup["id"],)).fetchone()
        changes = {}
        new_status = u.get("status")
        if new_status and new_status != role["status"]:
            changes["status"] = (role["status"], new_status)
        note = (u.get("note") or "").strip()
        if note:
            changes["notes"] = (role["notes"], (role["notes"] or "").rstrip() + f" [{note}]")
        if changes:
            matched.append((u, role, changes))
    return matched, unmatched, invalid


def print_plan(matched, unmatched, invalid):
    print(f"{len(matched)} role(s) to update, {len(unmatched)} unmatched, {len(invalid)} invalid entries.\n")
    for u, role, changes in matched:
        print(f"  [{role['id']}] {role['company']} / {role['title']}")
        for field, (old, new) in changes.items():
            print(f"      {field}: {old!r} -> {new!r}")
    if unmatched:
        print("\nUNMATCHED (not created):")
        for u in unmatched:
            print(f"  {u.get('company')!r} / {u.get('title')!r}")
    if invalid:
        print("\nINVALID (skipped):")
        for u, problems in invalid:
            print(f"  {u}: {'; '.join(problems)}")


def apply_plan(conn, matched):
    ts = dbmod.now()
    for u, role, changes in matched:
        sets = ", ".join(f"{f} = ?" for f in changes) + ", updated_at = ?"
        vals = [new for (_, new) in changes.values()] + [ts, role["id"]]
        conn.execute(f"UPDATE roles SET {sets} WHERE id = ?", vals)
    conn.commit()


# ------------------------------------------------------------------
# --create mode: staged, report-only creation. Never unattended — a row
# with no conflict still requires --create AND --apply together, and a row
# that matches an existing role is refused outright unless --force.
# ------------------------------------------------------------------

SOURCE_CHANNEL_LABELS = {
    "gmail": "Gmail sweep",
    "careers_page": "manual careers-page sweep",
}


def _provenance_label(row):
    """What actually found this row — never asserted, only what the row
    itself claims or can be inferred from. A bare source_email_date with no
    explicit source_channel still implies Gmail (that field only makes
    sense for an email), but absent both, this says "sweep", not a channel
    it has no evidence for."""
    channel = row.get("source_channel")
    if channel:
        return SOURCE_CHANNEL_LABELS.get(channel, channel)
    if row.get("source_email_date"):
        return "Gmail sweep"
    return "sweep"


def load_creates(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("creates file must be a JSON list of "
                          "{company, title, url, source_email_date, suggested_category} objects")
    return data


def validate_create(row):
    problems = []
    if not row.get("company"):
        problems.append("missing company")
    if not row.get("url"):
        problems.append("missing url")
    category = row.get("suggested_category")
    if category and category not in dbmod.CATEGORIES:
        problems.append(f"bad suggested_category: {category!r}")
    return problems


def _create_slug(company, title, used):
    base = "sweep-" + re.sub(r"[^a-z0-9]+", "-", f"{company}-{title or ''}".lower()).strip("-")
    base = base[:80]
    slug, i = base, 2
    while slug in used:
        slug = f"{base}-{i}"
        i += 1
    used.add(slug)
    return slug


def plan_creates(conn, creates, force=False):
    """Return (to_create, blocked, invalid). to_create is a list of row
    dicts ready for insertion. blocked is rows that matched an existing role
    and force=False — refused, not silently skipped, since a create path
    staying silent about a conflict is exactly the failure mode this whole
    thing exists to avoid."""
    to_create, blocked, invalid = [], [], []
    used_slugs = {r["job_id"] for r in conn.execute("SELECT job_id FROM roles") if r["job_id"]}
    for row in creates:
        problems = validate_create(row)
        if problems:
            invalid.append((row, problems))
            continue
        dup = app_module._find_duplicate(conn, row.get("company", ""), row.get("title", ""))
        if dup and not force:
            blocked.append((row, dup))
            continue
        slug = _create_slug(row.get("company", ""), row.get("title", ""), used_slugs)
        to_create.append({
            "job_id": slug,
            "company": row["company"],
            "title": row.get("title") or None,
            "url": row.get("url"),
            "category": row.get("suggested_category") or None,
            "source_email_date": row.get("source_email_date"),
            "source_channel": row.get("source_channel"),
            "forced_past": dup["id"] if (dup and force) else None,
        })
    return to_create, blocked, invalid


def print_plan_creates(to_create, blocked, invalid):
    print(f"{len(to_create)} role(s) to create, {len(blocked)} blocked by an existing match, "
          f"{len(invalid)} invalid entries.\n")
    for row in to_create:
        forced = f"  [forced past existing role #{row['forced_past']}]" if row["forced_past"] else ""
        print(f"  {row['company']} / {row['title'] or '(no title)'}{forced}")
        print(f"      url: {row['url']}")
        print(f"      source: {_provenance_label(row)}")
        if row["category"]:
            print(f"      category: {row['category']}")
        if row["source_email_date"]:
            print(f"      source email date: {row['source_email_date']}")
    if blocked:
        print("\nBLOCKED (matches an existing role — pass --force to create anyway):")
        for row, dup in blocked:
            print(f"  {row.get('company')!r} / {row.get('title')!r} matches existing role #{dup['id']} "
                  f"({dup['company']} / {dup['title']}, status={dup['status']})")
    if invalid:
        print("\nINVALID (skipped):")
        for row, problems in invalid:
            print(f"  {row}: {'; '.join(problems)}")


def apply_creates(conn, to_create):
    ts = dbmod.now()
    for row in to_create:
        notes = f"Found via {_provenance_label(row)} (--create)."
        if row["source_email_date"]:
            notes += f" Source email dated {row['source_email_date']}."
        if row["forced_past"]:
            notes += f" Forced past existing role #{row['forced_past']} (deliberate re-application)."
        conn.execute(
            """INSERT INTO roles (
                job_id, source, company, title, category, url, status, notes,
                jd_source, first_seen, last_seen, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["job_id"], "sweep", row["company"], row["title"], row["category"], row["url"],
             "sourced", notes, "none", dbmod.today(), dbmod.today(), ts, ts),
        )
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_file")
    ap.add_argument("--create", action="store_true",
                     help="treat input_file as staged new-role finds instead of updates")
    ap.add_argument("--apply", action="store_true",
                     help="write the changes (default: dry run, prints the diff only)")
    ap.add_argument("--force", action="store_true",
                     help="--create only: create rows even if they match an existing role")
    args = ap.parse_args()

    dest = dbmod.backup_db("gmail-sweep-apply")
    if dest:
        print(f"Backup: {dest}\n")

    conn = dbmod.connect()

    if args.create:
        creates = load_creates(args.input_file)
        to_create, blocked, invalid = plan_creates(conn, creates, force=args.force)
        print_plan_creates(to_create, blocked, invalid)
        if not args.apply:
            conn.close()
            print("\nDRY RUN — nothing written. Re-run with --create --apply to commit.")
            return 0
        if not to_create:
            conn.close()
            print("\nNothing to create.")
            return 0
        apply_creates(conn, to_create)
        conn.close()
        print(f"\nCreated {len(to_create)} role(s).")
        return 0

    updates = load_updates(args.input_file)
    matched, unmatched, invalid = plan(conn, updates)
    print_plan(matched, unmatched, invalid)

    if not args.apply:
        conn.close()
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0
    if not matched:
        conn.close()
        print("\nNothing to apply.")
        return 0

    apply_plan(conn, matched)
    conn.close()
    print(f"\nApplied {len(matched)} update(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
