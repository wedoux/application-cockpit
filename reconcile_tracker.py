#!/usr/bin/env python3
"""
tracker.md -> roles reconciliation (Phase 0.5).
===============================================
Imports the hand-maintained pipeline (job-search/tracker.md) into the roles
table, merging on company+title. tracker.md is the curated source of truth for
the pipeline fields (interest / channel / status / next_action / applied /
due / notes), so on a match those values win; where there is no match, the
tracker row is inserted as a new manual role.

SAFETY: default mode is --preview (writes nothing). It prints a summary and
writes a full proposal to a markdown file for eyeballing. Only --apply mutates
the database, and only after you've reviewed the preview.

Matching:
  * company + title normalized (lowercase, punctuation stripped, spaces
    collapsed). Equal normalized strings = EXACT.
  * same company, title similarity >= FUZZY_THRESHOLD = FUZZY (needs eyeball).
  * anything else = NEW (insert as a manual role).

Conflict guard: if the DB role is live (status sourced/reviewing) but the
tracker row is dead (rejected/expired/ignored), we do NOT auto-merge — that
would bury a live repost under an old rejection. Flagged for a human call.

Usage:
  python3 reconcile_tracker.py               # preview only, no writes
  python3 reconcile_tracker.py --apply        # perform the merges + inserts
  python3 reconcile_tracker.py --pipeline-closed=expired   # override mapping
"""

import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

import db as dbmod
from job_scanner import categorize_title

TRACKER = Path.home() / "Claude" / "job-search" / "tracker.md"
PREVIEW_OUT = Path("/private/tmp/claude-501/-Users-niko-Claude-Projects-Job-hunting/"
                   "fdf88d9e-db86-4b10-bf82-575f2d90009f/scratchpad/"
                   "tracker-reconcile-preview.md")
FUZZY_THRESHOLD = 0.60
# Below FUZZY_THRESHOLD but same company + this much title overlap = a near-miss
# worth surfacing (default: insert as new, but flagged for a merge decision).
POSSIBLE_LOWER = 0.30
URL_RE = re.compile(r"https?://[^\s)|\]]+")

# tracker status -> unified vocab.
STATUS_MAP = {
    "sourced": "sourced", "drafted": "drafted", "submitted": "submitted",
    "interview": "interview", "offer": "offer",
}
DEAD = {"rejected", "expired", "ignored"}
# pipeline "closed" is ambiguous (posting gone vs. you declined it). Default
# and overridable via --pipeline-closed=.
PIPELINE_CLOSED_DEFAULT = "expired"

# ---- Human decisions from the Phase 0.5 preview sign-off ----
# Keyed by (normalized company, normalized title). These make the one-off
# reconciliation reproducible and auditable instead of hand-editing the DB.

# Per-row status overrides (Danske was declined on geography, not lapsed).
STATUS_OVERRIDE = {
    ("danske bank", "head of human centered design center of excellence"): "ignored",
}
# Possible-match rows to force-merge into an existing role (same role, different
# title text). Value = normalized title of the existing role to merge into.
FORCE_MERGE = {
    ("logitech", "sr ux designer logitech g req 145142"): "sr user experience designer",
}
# Conflicts: import the tracker (historical) row as its own row, and stamp a
# note on the live DB role so the signal shows there too.
IMPORT_CONFLICTS_AS_NEW = True
CONFLICT_LIVE_NOTE = {
    ("nexthink", "senior product designer"): "Repost of role rejected 08.05.",
}


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(v):
    v = (v or "").strip()
    if v in ("", "—", "-", "–", "~", "?"):
        return None
    return v.lstrip("~").strip() or None


def score_of(v):
    try:
        return float((v or "").strip())
    except (TypeError, ValueError):
        return None


def extract_url(*texts):
    for t in texts:
        m = URL_RE.search(t or "")
        if m:
            return m.group(0).rstrip(".,")
    return None


# ------------------------------------------------------------------
# Parse tracker.md
# ------------------------------------------------------------------

def _is_sep(cells):
    return all(set(c) <= set("-: ") and c for c in cells)


def parse_tracker(path=TRACKER):
    section, headers = None, None
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s.startswith("## "):
            name = s[3:].strip().lower()
            section = ("pipeline" if name.startswith("pipeline")
                       else "closed" if name.startswith("closed") else None)
            headers = None
            continue
        if not section or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if _is_sep(cells):
            continue
        if headers is None:
            headers = [c.lower().strip() for c in cells]
            continue
        rows.append(_to_row(dict(zip(headers, cells)), section))
    return rows


def _to_row(d, section, pipeline_closed=PIPELINE_CLOSED_DEFAULT):
    company = d.get("company", "").strip()
    title = d.get("role", "").strip()
    notes = d.get("notes", "")
    if section == "closed":
        outcome = d.get("outcome", "")
        status = "rejected" if "reject" in outcome.lower() else "expired"
        return {
            "section": "closed", "company": company, "title": title,
            "status": status, "score": None, "interest": None, "channel": None,
            "next_action": None, "applied_date": None,
            "due": clean(d.get("closed")), "notes": notes or outcome,
            "url": extract_url(notes), "raw_status": outcome,
        }
    raw_status = d.get("status", "").strip().lower()
    status = STATUS_MAP.get(raw_status) or (pipeline_closed if raw_status == "closed" else "sourced")
    return {
        "section": "pipeline", "company": company, "title": title,
        "status": status, "score": score_of(d.get("score")),
        "interest": clean(d.get("interest")), "channel": clean(d.get("channel")),
        "next_action": clean(d.get("next action")),
        "applied_date": clean(d.get("applied")), "due": clean(d.get("due")),
        "notes": notes, "url": extract_url(notes, d.get("next action")),
        "raw_status": raw_status,
    }


# ------------------------------------------------------------------
# Match against existing roles
# ------------------------------------------------------------------

def match_row(trow, roles):
    """Return (kind, role_or_None, ratio). kind in exact|fuzzy|new."""
    same_company = [r for r in roles if norm(r["company"]) == norm(trow["company"])]
    best, best_ratio = None, 0.0
    for r in same_company:
        ratio = SequenceMatcher(None, norm(r["title"]), norm(trow["title"])).ratio()
        if ratio > best_ratio:
            best, best_ratio = r, ratio
    if best is None:
        return "new", None, 0.0
    if best_ratio >= 0.999:
        return "exact", best, best_ratio
    if best_ratio >= FUZZY_THRESHOLD:
        return "fuzzy", best, best_ratio
    if best_ratio >= POSSIBLE_LOWER:
        return "possible", best, best_ratio
    return "new", None, best_ratio


def is_conflict(trow, role):
    return role["status"] in ("sourced", "reviewing") and trow["status"] in DEAD


def merged_fields(trow, role):
    """Fields that would change on the DB role, as {col: (old, new)}."""
    changes = {}

    def maybe(col, new):
        old = role[col]
        if new is not None and str(new) != str(old or ""):
            changes[col] = (old, new)

    maybe("status", trow["status"])
    if trow["score"] is not None and role["score"] is None:
        changes["score"] = (role["score"], trow["score"])
    for col in ("interest", "channel", "next_action", "applied_date", "due"):
        maybe(col, trow[col])
    # notes: prefer tracker, preserve the old scanner reason.
    if trow["notes"]:
        old = role["notes"]
        new = trow["notes"]
        if old and old not in new:
            new = f"{new}\n\n[from scanner CSV] {old}"
        if new != (old or ""):
            changes["notes"] = (old, new)
    if not role["url"] and trow["url"]:
        changes["url"] = (role["url"], trow["url"])
    if role["category"] == "Review":
        changes["category"] = (role["category"], categorize_title(trow["title"]))
    return changes


# ------------------------------------------------------------------
# Build the plan
# ------------------------------------------------------------------

def build_plan(pipeline_closed=PIPELINE_CLOSED_DEFAULT):
    conn = dbmod.connect()
    roles = [dict(r) for r in conn.execute("SELECT * FROM roles")]
    conn.close()

    rows = parse_tracker()
    # Re-map pipeline "closed" if overridden.
    if pipeline_closed != PIPELINE_CLOSED_DEFAULT:
        for r in rows:
            if r["section"] == "pipeline" and r["raw_status"] == "closed":
                r["status"] = pipeline_closed

    merges, fuzzies, conflicts, possibles, news = [], [], [], [], []
    used_slugs = {r["job_id"] for r in roles if r["job_id"]}

    for t in rows:
        kind, role, ratio = match_row(t, roles)
        if kind in ("exact", "fuzzy"):
            if is_conflict(t, role):
                conflicts.append((t, role, ratio))
            elif kind == "exact":
                merges.append((t, role, ratio, merged_fields(t, role)))
            else:
                fuzzies.append((t, role, ratio, merged_fields(t, role)))
            continue
        # Not a confident match: insert as a new manual role. If the same company
        # already exists, surface it as a near-miss so a merge can be chosen.
        t["_slug"] = _slug(t, used_slugs)
        t["_category"] = categorize_title(t["title"])
        news.append(t)
        if kind == "possible":
            possibles.append((t, role, ratio))

    _apply_decisions(merges, fuzzies, possibles, news, roles)
    return {"merges": merges, "fuzzies": fuzzies, "conflicts": conflicts,
            "possibles": possibles, "news": news, "n_roles": len(roles)}


def _apply_decisions(merges, fuzzies, possibles, news, roles):
    """Fold the human sign-off decisions into the plan."""
    # 1. Per-row status overrides on new rows.
    for t in news:
        key = (norm(t["company"]), norm(t["title"]))
        if key in STATUS_OVERRIDE:
            t["status"] = STATUS_OVERRIDE[key]

    # 2. Force-merge chosen possibles into an existing role.
    for tkey, target_title in FORCE_MERGE.items():
        t = next((x for x in news
                  if (norm(x["company"]), norm(x["title"])) == tkey), None)
        role = next((r for r in roles
                     if norm(r["company"]) == tkey[0]
                     and norm(r["title"]) == target_title), None)
        if t is None or role is None:
            continue
        news.remove(t)
        possibles[:] = [p for p in possibles if p[0] is not t]
        fuzzies.append((t, role, 1.0, merged_fields(t, role)))


def _slug(t, used):
    base = "trk-" + norm(t["company"]).replace(" ", "-") + "-" + norm(t["title"]).replace(" ", "-")
    base = base[:80]
    slug, i = base, 2
    while slug in used:
        slug = f"{base}-{i}"
        i += 1
    used.add(slug)
    return slug


# ------------------------------------------------------------------
# Preview
# ------------------------------------------------------------------

def _fmt_changes(changes):
    out = []
    for col, (old, new) in changes.items():
        o = (str(old)[:60] + "…") if old and len(str(old)) > 60 else (old or "∅")
        n = (str(new)[:60] + "…") if new and len(str(new)) > 60 else new
        out.append(f"    - {col}: `{o}` → `{n}`")
    return "\n".join(out) if out else "    - (no field changes)"


def write_preview(plan, pipeline_closed):
    L = []
    L.append("# tracker.md → roles — reconciliation preview\n")
    L.append(f"Existing roles in DB: **{plan['n_roles']}**. Nothing below is written "
             "until you approve and I run `--apply`.\n")
    L.append(f"Pipeline `closed` rows are mapped to **{pipeline_closed}** "
             "(override with `--pipeline-closed=ignored`).\n")

    L.append("\n## A. Exact matches → merge into existing role\n")
    if not plan["merges"]:
        L.append("_None._")
    for t, r, ratio, ch in plan["merges"]:
        L.append(f"- **[{r['company']}] {r['title']}** (role id {r['id']}, "
                 f"db status `{r['status']}`) ⇐ tracker `{t['section']}` "
                 f"row `{t['title']}` (status `{t['status']}`)")
        L.append(_fmt_changes(ch))

    L.append("\n## B. Fuzzy matches → merge, PLEASE EYEBALL\n")
    if not plan["fuzzies"]:
        L.append("_None._")
    for t, r, ratio, ch in plan["fuzzies"]:
        L.append(f"- **[{r['company']}] {r['title']}** (role id {r['id']}, "
                 f"db status `{r['status']}`) ⇐ tracker `{t['title']}`  "
                 f"— title similarity **{ratio:.0%}**")
        L.append(_fmt_changes(ch))

    L.append("\n## B2. Possible matches → default INSERT as new, tell me to merge instead\n")
    L.append("Same company as an existing role, but the title is too different to merge "
             "automatically. Default action is **insert as a separate row**. If any of "
             "these is really the same role, say so and I'll merge it instead.\n")
    if not plan["possibles"]:
        L.append("_None._")
    for t, r, ratio in plan["possibles"]:
        L.append(f"- tracker `{t['title']}` (status `{t['status']}`) vs existing "
                 f"**[{r['company']}] {r['title']}** (role id {r['id']}, `{r['status']}`) "
                 f"— title similarity **{ratio:.0%}** → default: INSERT new")

    L.append("\n## C. Conflicts → NOT merged, need your call\n")
    if not plan["conflicts"]:
        L.append("_None._")
    for t, r, ratio in plan["conflicts"]:
        L.append(f"- **[{r['company']}] {r['title']}** — DB role id {r['id']} is "
                 f"**live** (`{r['status']}`) but tracker row is **{t['status']}** "
                 f"(`{t['section']}`, sim {ratio:.0%}).")
        L.append(f"    - Merging would mark the live role `{t['status']}`. "
                 "The tracker row is an earlier posting instance (different job_id).")
        L.append("    - Options: (a) skip — keep live role, drop tracker row; "
                 "(b) import tracker row as a separate historical row. Default: (a).")

    L.append(f"\n## D. New rows → insert as manual roles ({len(plan['news'])})\n")
    L.append("| Company | Title | Status | Interest | Score | Category | Section |")
    L.append("|---|---|---|---|---|---|---|")
    for t in sorted(plan["news"], key=lambda x: (x["section"], x["company"])):
        L.append(f"| {t['company']} | {t['title'][:48]} | {t['status']} | "
                 f"{t['interest'] or '—'} | {t['score'] if t['score'] is not None else '—'} | "
                 f"{t['_category']} | {t['section']} |")

    n_merge = len(plan["merges"]) + len(plan["fuzzies"])
    L.append(f"\n## Summary\n")
    L.append(f"- Merge into existing: **{n_merge}** "
             f"({len(plan['merges'])} exact, {len(plan['fuzzies'])} fuzzy)")
    L.append(f"- Conflicts to resolve: **{len(plan['conflicts'])}**")
    L.append(f"- New roles to insert: **{len(plan['news'])}**")
    L.append(f"- Roles after apply (if all inserts go in): "
             f"**{plan['n_roles'] + len(plan['news'])}**")

    PREVIEW_OUT.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    return "\n".join(L)


# ------------------------------------------------------------------
# Apply (only with --apply, after approval)
# ------------------------------------------------------------------

def apply_plan(plan, include_conflicts_as_new=False):
    dbmod.backup_db("tracker-reconcile")
    conn = dbmod.connect()
    ts = dbmod.now()
    merged = inserted = 0

    for group in (plan["merges"], plan["fuzzies"]):
        for t, r, ratio, ch in group:
            if not ch:
                continue
            sets = ", ".join(f"{c} = ?" for c in ch) + ", updated_at = ?"
            vals = [new for (_, new) in ch.values()] + [ts, r["id"]]
            conn.execute(f"UPDATE roles SET {sets} WHERE id = ?", vals)
            merged += 1

    to_insert = list(plan["news"])
    if include_conflicts_as_new:
        for t, r, ratio in plan["conflicts"]:
            used = {row["job_id"] for row in conn.execute("SELECT job_id FROM roles")}
            t["_slug"] = _slug(t, used)
            t["_category"] = categorize_title(t["title"])
            to_insert.append(t)
            # Stamp a note on the live DB role so the signal shows there too.
            note = CONFLICT_LIVE_NOTE.get((norm(t["company"]), norm(t["title"])))
            if note:
                cur_note = r["notes"] or ""
                if note not in cur_note:
                    new_note = f"{cur_note} [{note}]".strip()
                    conn.execute(
                        "UPDATE roles SET notes = ?, updated_at = ? WHERE id = ?",
                        (new_note, ts, r["id"]),
                    )

    for t in to_insert:
        conn.execute(
            """INSERT INTO roles (
                job_id, source, company, title, category, score, url, status,
                interest, channel, next_action, applied_date, due, notes,
                jd_source, first_seen, last_seen, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["_slug"], "manual", t["company"], t["title"], t["_category"],
             t["score"], t["url"], t["status"], t["interest"], t["channel"],
             t["next_action"], t["applied_date"], t["due"], t["notes"], "none",
             t["applied_date"] or dbmod.today(), dbmod.today(), ts, ts),
        )
        inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()
    return {"merged": merged, "inserted": inserted, "total": total}


def main():
    pipeline_closed = PIPELINE_CLOSED_DEFAULT
    for a in sys.argv:
        if a.startswith("--pipeline-closed="):
            pipeline_closed = a.split("=", 1)[1]

    plan = build_plan(pipeline_closed)

    if "--apply" in sys.argv:
        res = apply_plan(plan, include_conflicts_as_new=IMPORT_CONFLICTS_AS_NEW)
        print(f"Applied: {res['merged']} merged, {res['inserted']} inserted, "
              f"{res['total']} total roles.")
        return

    write_preview(plan, pipeline_closed)
    n_merge = len(plan["merges"]) + len(plan["fuzzies"])
    print("PREVIEW ONLY — nothing written.")
    print(f"  Exact merges:      {len(plan['merges'])}")
    print(f"  Fuzzy merges:      {len(plan['fuzzies'])}  (eyeball these)")
    print(f"  Possible matches:  {len(plan['possibles'])}  (same company, default insert-new)")
    print(f"  Conflicts:         {len(plan['conflicts'])}  (need your call)")
    print(f"  New inserts:       {len(plan['news'])}")
    print(f"  Roles now / after: {plan['n_roles']} / {plan['n_roles'] + len(plan['news'])}")
    print(f"\nFull proposal written to:\n  {PREVIEW_OUT}")


if __name__ == "__main__":
    main()
