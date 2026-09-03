#!/usr/bin/env python3
"""
Staged-finds ingest.
=====================
The three scheduled sourcing tasks run in a sandbox where this project
folder is a network-style mount. SQLite can't take the file locks it needs
there — every commit dies with "disk I/O error" and leaves an orphan
-journal behind — so those tasks cannot write to cockpit.db at all. They
stage a JSON file into ../staging/ and stop.

Until now the second half was a hand-typed command, and it had three
failure modes in a single day: bare python3 (no venv → ModuleNotFoundError:
dotenv), the wrong staging path (the task prompts name a directory the
sandbox can't actually reach), and — worst — silence, because a file nobody
remembers to apply just rots in a folder. This module is the machine-driven
half: find the files, show the diff, apply the rows that were picked.

It owns no rules of its own. Planning, duplicate detection and writing all
belong to gmail_sweep_apply.py, whose plan/apply split already separates
"what would happen" from "do it" — that's what makes this a thin layer.
In particular duplicate detection stays app._find_duplicate, the same
normalized company+title rule the manual-add button uses; a second copy of
that rule here would drift from it and quietly change what counts as a
duplicate depending on which door a role came through.

The HTTP routes in app.py are a wrapper over these functions, not the other
way round, so an unattended launchd job can call ingest() directly later
without a Flask server in the middle.

Two file shapes land in the same folder and the filenames don't reliably
say which is which, so mode is sniffed from the keys, never guessed:
a row carrying status/note is an update, a row carrying url is a create.
Anything mixed or unrecognised is reported as an error — a folder whose
whole purpose is to stop silent failure is the last place to guess.
"""

import json
from datetime import datetime
from pathlib import Path

import app as app_module
import db as dbmod
import gmail_sweep_apply as gsa
import prompt_assembly as pa

REPO_DIR = Path(__file__).parent

# Where the scheduled tasks drop their JSON. Real paths live in config.yaml
# (gitignored) and never in a tracked source file — same rule as every other
# path in this project, and the reason config.yaml.example points at
# ./sample-profile/ so a stranger who clones this gets something that runs.
# It matters here because the directory the tasks actually reach is NOT the
# one their prompts name, which is half of why the hand-typed command kept
# failing; that discrepancy is a fact about one machine's layout, so it
# belongs in config, not in this file.
DEFAULT_STAGING_DIR = "./staging"


def _configured_staging_dir():
    """paths.staging_dir from config.yaml, resolved against the repo so a
    relative value in the committed example works on a fresh clone. An
    absolute value overrides that, pathlib-style.

    Falls back to the default rather than raising: a missing or malformed
    config must not take the whole cockpit down at import time over a
    directory this module can perfectly well guess.
    """
    configured = None
    try:
        configured = ((pa.load_config() or {}).get("paths") or {}).get("staging_dir")
    except Exception as exc:                                    # noqa: BLE001
        print(f"[staging] couldn't read paths.staging_dir from config.yaml ({exc}); "
              f"falling back to {DEFAULT_STAGING_DIR}")
    return (REPO_DIR / (configured or DEFAULT_STAGING_DIR)).resolve()


# Read once at import, then referenced inside functions and never bound as a
# default argument — same call-time-resolution rule as db.DB_PATH and
# app.APPLICATIONS_DIR, which is what lets the tests redirect it.
STAGING_DIR = _configured_staging_dir()

APPLIED_SUFFIX = ".applied.json"

MODE_CREATE = "create"
MODE_UPDATE = "update"
MODE_EMPTY = "empty"

# A row carrying either of these is an update; a row carrying "url" is a
# create. Kept as data rather than inline literals so the error messages
# below and the sniffing stay in step.
UPDATE_KEYS = {"status", "note"}
CREATE_KEYS = {"url"}


class StagedPathError(ValueError):
    """A path that doesn't resolve to a real JSON file directly inside the
    staging directory. Raised rather than returned because a caller that
    got here is either malformed or hostile — there's no partial result
    worth rendering."""


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

def resolve_staged_path(raw):
    """Resolve `raw` to a file inside STAGING_DIR, or raise.

    Everything is resolved before comparison, so a traversal
    ("../../etc/passwd"), an absolute path elsewhere, and a symlink planted
    inside the staging folder that points out of it all fail the same
    check. Only direct children count — no subdirectories — because the
    tasks only ever write flat into this folder.
    """
    base = Path(STAGING_DIR).resolve()
    candidate = Path(raw)
    if not str(candidate):
        raise StagedPathError("no path given")
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if resolved.parent != base:
        raise StagedPathError("path is outside the staging directory")
    if resolved.suffix != ".json":
        raise StagedPathError("not a .json file")
    if not resolved.is_file():
        raise StagedPathError("no such staged file")
    return resolved


def _is_applied(path):
    return path.name.endswith(APPLIED_SUFFIX)


def mark_applied(path):
    """Rename <name>.json -> <name>.applied.json so it stops showing as
    pending. Convenience, not a safety mechanism: re-applying a file is
    already harmless because every row comes back BLOCKED as a duplicate.
    A rename needs no new state store, survives a database restore, and is
    visible in Finder — which a row in a table would not be."""
    path = Path(path)
    if _is_applied(path):
        return path
    stem = path.name[: -len(".json")]
    target = path.with_name(stem + APPLIED_SUFFIX)
    n = 2
    while target.exists():
        target = path.with_name(f"{stem}-{n}{APPLIED_SUFFIX}")
        n += 1
    path.rename(target)
    return target


# ------------------------------------------------------------------
# Reading and mode detection
# ------------------------------------------------------------------

def _load(path):
    """Return (rows, error). Never raises on a malformed file — a broken
    drop is one row in the panel with a readable reason, not a 500 that
    hides the other files."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"can't read the file: {exc}"
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(data, list):
        return None, "not a JSON list of objects"
    return data, None


def _row_mode(row):
    if not isinstance(row, dict):
        return "unknown"
    update_like = bool(UPDATE_KEYS & row.keys())
    create_like = bool(CREATE_KEYS & row.keys())
    if update_like and create_like:
        return "both"
    if update_like:
        return MODE_UPDATE
    if create_like:
        return MODE_CREATE
    return "unknown"


def _row_list(indices):
    return ", ".join(str(i + 1) for i in indices)


def detect_mode(rows):
    """Return (mode, error) for a parsed file.

    An empty list is MODE_EMPTY, not an error: a sweep that found nothing
    legitimately stages []. Everything else must agree — one unrecognised
    row makes the whole file an error rather than letting the majority
    shape decide what happens to it.
    """
    if not rows:
        return MODE_EMPTY, None
    seen = {}
    for i, row in enumerate(rows):
        seen.setdefault(_row_mode(row), []).append(i)
    problems = []
    if "both" in seen:
        problems.append(f"row(s) {_row_list(seen['both'])} carry both a url and a status/note")
    if "unknown" in seen:
        problems.append(f"row(s) {_row_list(seen['unknown'])} carry neither a url nor a status/note")
    if MODE_CREATE in seen and MODE_UPDATE in seen:
        problems.append(f"row(s) {_row_list(seen[MODE_CREATE])} look like creates but "
                        f"row(s) {_row_list(seen[MODE_UPDATE])} look like updates")
    if problems:
        return None, ("Can't tell what this file is: " + "; ".join(problems)
                      + ". Fix the file or split it in two — nothing is guessed here.")
    return next(iter(seen)), None


def list_staged(include_applied=False):
    """One entry per staged JSON file, newest first. Never opens the
    database — this is a directory listing plus a key sniff."""
    base = Path(STAGING_DIR)
    if not base.is_dir():
        return []
    entries = []
    for path in base.glob("*.json"):
        applied = _is_applied(path)
        if applied and not include_applied:
            continue
        rows, error = _load(path)
        mode = None
        if error is None:
            mode, error = detect_mode(rows)
        entries.append({
            "path": str(path),
            "filename": path.name,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "mode": mode,
            "rows": 0 if rows is None else len(rows),
            "applied": applied,
            "error": error,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


# ------------------------------------------------------------------
# Preview — the structured equivalent of print_plan_creates / print_plan
# ------------------------------------------------------------------
#
# Every previewed row carries "index": its position in the source JSON
# file, NOT its position in the plan. Preview and apply are two separate
# requests, each re-planning against a live database; if the Monday cron or
# a manual add lands between them, the create/blocked split shifts and a
# positional selection would silently apply to the wrong rows. The source
# file doesn't move, so that's what the checkboxes are keyed to.

def _index_rows(rows, buckets):
    """Walk the source rows in order and hand each one to its bucket
    handler. plan()/plan_creates() put the ORIGINAL row object in their
    unmatched/blocked/invalid lists, so those are matched by identity;
    whatever's left came out as a planned row, and those preserve input
    order, so they're consumed in sequence."""
    for i, row in enumerate(rows):
        for match, handler in buckets:
            if match(row):
                handler(i, row)
                break


def _preview_creates(conn, rows):
    to_create, blocked, invalid = gsa.plan_creates(conn, rows)
    invalid_by_id = {id(r): problems for r, problems in invalid}
    blocked_by_id = {id(r): dup for r, dup in blocked}
    planned = iter(to_create)

    creates_out, blocked_out, invalid_out = [], [], []

    def on_invalid(i, row):
        invalid_out.append({"index": i, "row": row, "problems": invalid_by_id[id(row)]})

    def on_blocked(i, row):
        dup = blocked_by_id[id(row)]
        blocked_out.append({
            "index": i,
            "company": row.get("company"),
            "title": row.get("title"),
            "existing": {k: dup[k] for k in ("id", "company", "title", "status")},
        })

    def on_create(i, row):
        p = next(planned)
        creates_out.append({
            "index": i,
            "company": p["company"],
            "title": p["title"],
            "url": p["url"],
            "category": p["category"],
            "source": gsa._provenance_label(p),
            "source_email_date": p["source_email_date"],
        })

    _index_rows(rows, [
        (lambda r: id(r) in invalid_by_id, on_invalid),
        (lambda r: id(r) in blocked_by_id, on_blocked),
        (lambda r: True, on_create),
    ])
    return {"to_create": creates_out, "blocked": blocked_out, "invalid": invalid_out}


def _preview_updates(conn, rows):
    matched, unmatched, invalid = gsa.plan(conn, rows)
    matched_by_id = {id(u): (role, changes) for u, role, changes in matched}
    invalid_by_id = {id(u): problems for u, problems in invalid}
    unmatched_ids = {id(u) for u in unmatched}

    updates_out, unmatched_out, invalid_out, no_change_out = [], [], [], []

    def on_invalid(i, row):
        invalid_out.append({"index": i, "row": row, "problems": invalid_by_id[id(row)]})

    def on_unmatched(i, row):
        unmatched_out.append({"index": i, "company": row.get("company"), "title": row.get("title")})

    def on_matched(i, row):
        role, changes = matched_by_id[id(row)]
        updates_out.append({
            "index": i,
            "role_id": role["id"],
            "company": role["company"],
            "title": role["title"],
            "changes": [{"field": f, "old": old, "new": new} for f, (old, new) in changes.items()],
        })

    def on_no_change(i, row):
        # plan() returns a matched row ONLY when it has changes, so a row
        # that matched an existing role already holding that status falls
        # out of all three lists and the CLI never prints it. Fine in a
        # terminal where you can count; not fine in a panel, where a row
        # vanishing between the file and the diff is exactly the silence
        # this whole thing exists to remove.
        dup = app_module._find_duplicate(conn, row.get("company", ""), row.get("title", ""))
        no_change_out.append({
            "index": i,
            "company": row.get("company"),
            "title": row.get("title"),
            "role_id": dup["id"] if dup else None,
        })

    _index_rows(rows, [
        (lambda r: id(r) in invalid_by_id, on_invalid),
        (lambda r: id(r) in unmatched_ids, on_unmatched),
        (lambda r: id(r) in matched_by_id, on_matched),
        (lambda r: True, on_no_change),
    ])
    return {"to_update": updates_out, "unmatched": unmatched_out,
            "invalid": invalid_out, "no_change": no_change_out}


def preview(path):
    """Structured plan for one staged file. Read-only — no backup, no
    write, no rename."""
    path = resolve_staged_path(path)
    base = {"path": str(path), "filename": path.name}
    rows, error = _load(path)
    if error:
        return {**base, "mode": None, "rows": 0, "error": error}
    mode, error = detect_mode(rows)
    if error:
        return {**base, "mode": None, "rows": len(rows), "error": error}
    out = {**base, "mode": mode, "rows": len(rows), "error": None}
    if mode == MODE_EMPTY:
        return out
    conn = dbmod.connect()
    try:
        detail = _preview_creates(conn, rows) if mode == MODE_CREATE else _preview_updates(conn, rows)
    finally:
        conn.close()
    out.update(detail)
    return out


# ------------------------------------------------------------------
# Apply
# ------------------------------------------------------------------

def ingest(path, selection=None):
    """Apply the selected rows of one staged file, then mark it applied.

    `selection` is a list of source-file indices (see the note above
    _index_rows for why not plan positions); None means every row. The
    selected rows are re-planned from scratch here rather than carried over
    from the preview, so a role that became a duplicate in the meantime is
    caught by the same rule as everything else instead of being created on
    the strength of a stale plan.

    The file is marked applied even when rows were left unchecked: an
    unchecked row is a decision, not unfinished work. Leaving the file
    pending because you deliberately dropped something would mean the
    count never clears.
    """
    path = resolve_staged_path(path)
    rows, error = _load(path)
    if error:
        return {"ok": False, "error": error, "filename": path.name}
    mode, error = detect_mode(rows)
    if error:
        return {"ok": False, "error": error, "filename": path.name}

    if selection is None:
        chosen = list(range(len(rows)))
    else:
        chosen = sorted({i for i in selection if isinstance(i, int) and 0 <= i < len(rows)})
    subset = [rows[i] for i in chosen]

    result = {"ok": True, "mode": mode, "filename": path.name,
              "selected": len(chosen), "skipped": len(rows) - len(chosen),
              "created": 0, "updated": 0, "blocked": 0, "unmatched": 0, "invalid": 0}

    if subset:
        conn = dbmod.connect()
        try:
            if mode == MODE_CREATE:
                to_create, blocked, invalid = gsa.plan_creates(conn, subset)
                result["blocked"] = len(blocked)
                result["invalid"] = len(invalid)
                if to_create:
                    dbmod.backup_db("ingest-panel")
                    gsa.apply_creates(conn, to_create)
                    result["created"] = len(to_create)
            elif mode == MODE_UPDATE:
                matched, unmatched, invalid = gsa.plan(conn, subset)
                result["unmatched"] = len(unmatched)
                result["invalid"] = len(invalid)
                if matched:
                    dbmod.backup_db("ingest-panel")
                    gsa.apply_plan(conn, matched)
                    result["updated"] = len(matched)
        finally:
            conn.close()

    result["applied_path"] = str(mark_applied(path))
    return result
