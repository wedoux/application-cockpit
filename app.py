#!/usr/bin/env python3
"""
Application Cockpit — Flask core.
==================================
Local, single-user. Source -> gate -> generate -> verify -> render, backed by
SQLite (cockpit.db). Bound to 127.0.0.1 only; nothing here ever submits an
application on its own.

Usage:
    python3 app.py             # serve http://127.0.0.1:8766 and open it
    python3 app.py --no-open   # don't auto-open the browser
"""

import hashlib
import json
import re
import subprocess
import sys
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

import ats_verify
import cv_render
import db as dbmod
import generation
import jd_fetch
import language_gate
import cv_order
import cv_schema
import numeric_fact_gate as ng
import numeric_repair
import pdf_export
import prompt_assembly as pa
import staging
import style_gate
import verifier
from job_scanner import scan_all_companies_detailed, categorize_title

HOST = "127.0.0.1"
PORT = 8766

# Rendered PDFs land under here (spec section 4: applications/<company>-<role>/).
# Referenced inside functions, never bound as a default argument — see db.py's
# history for exactly why that distinction matters for test isolation.
APPLICATIONS_DIR = Path(__file__).parent / "applications"

app = Flask(__name__)

# Schema creation used to happen only in main(), before app.run() — fine for
# `python3 app.py`, but silent data-loss-by-500 for any other way of serving
# this app (a WSGI server that imports `app.app` directly and never calls
# main()): the first DB-touching route would fail with "no such table"
# instead of the schema just existing. Guaranteed here instead, on the first
# request through any path — cheap (one flag check) on every request after
# that. init_db() itself is idempotent (CREATE TABLE IF NOT EXISTS), so
# main() below still calling it too is harmless, not a second source of truth.
_db_ready = False


@app.before_request
def _ensure_db_ready():
    global _db_ready
    if not _db_ready:
        dbmod.init_db()
        _db_ready = True


# ------------------------------------------------------------------
# Roles
# ------------------------------------------------------------------

def role_to_dict(row):
    return {k: row[k] for k in row.keys()}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/roles")
def api_roles():
    conn = dbmod.connect()
    rows = conn.execute(
        "SELECT * FROM roles WHERE merged_into IS NULL ORDER BY "
        "CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC, company"
    ).fetchall()
    conn.close()
    return jsonify({
        "roles": [role_to_dict(r) for r in rows],
        "statuses": dbmod.STATUSES,
        "categories": dbmod.CATEGORIES,
        "interests": dbmod.INTERESTS,
        "decision_reasons": dbmod.DECISION_REASONS,
    })


def _manual_slug(company, title):
    base = "manual-" + re.sub(r"[^a-z0-9]+", "-", f"{company}-{title or ''}".lower()).strip("-")
    return base[:80]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find_duplicate(conn, company, title):
    """Soft-duplicate match: normalized company+title against every existing
    role, auto or manual — a manual add of a scanner-sourced role has a
    different job_id, so an exact job_id check (the old behaviour) misses it.
    Skipped when title is blank: company-only matching is too broad."""
    t = _norm(title)
    if not t:
        return None
    c = _norm(company)
    for r in conn.execute(
        "SELECT id, company, title, location, status FROM roles WHERE merged_into IS NULL"
    ):
        if _norm(r["company"]) == c and _norm(r["title"]) == t:
            return r
    return None


@app.post("/api/roles")
def api_add_role():
    """Manual add (spec section 8). Accepts company + at least one of url / jd_text.
    A normalized company+title match is a soft duplicate: 409 with the suspected
    row unless the caller passes force=true (spec section 1 — nothing is a dead
    end). A fetched JD is never persisted here — it comes back as fetch_preview;
    the client must call /fetch_jd/accept to store it, so a JS-shell page can't
    reach generation unseen."""
    body = request.get_json(silent=True) or {}
    company = (body.get("company") or "").strip()
    title = (body.get("title") or "").strip()
    url = (body.get("url") or "").strip()
    jd_text = (body.get("jd_text") or "").strip()
    category = (body.get("category") or "").strip()
    force = bool(body.get("force"))

    if not company:
        return jsonify({"ok": False, "error": "company_required",
                        "message": "Company is required."}), 400
    if not url and not jd_text:
        return jsonify({"ok": False, "error": "need_url_or_jd",
                        "message": "Give a URL or paste the JD (or both)."}), 400

    # Resolve the JD. A paste is a deliberate act — you typed or pasted it
    # yourself — so it stores immediately. A fetch is automated and can pull
    # the wrong page or a JS shell, so it doesn't: it comes back as a
    # preview below for you to accept first.
    jd_source = "paste" if jd_text else "none"
    fetch_preview = None
    if not jd_text and url:
        fetched_text, fetched_title, fetched_source = jd_fetch.fetch_jd(url)
        if not title and fetched_title:
            title = fetched_title
        if fetched_source in ("fetch", "browser") and fetched_text:
            title_check = jd_fetch.title_words_present(title, fetched_text)
            fetch_preview = {"chars": len(fetched_text),
                             "preview": fetched_text[:500],
                             "text": fetched_text,
                             "title_check": title_check,
                             "title_mismatch": title_check["total"] > 0 and title_check["hits"] == 0}

    if not category:
        category = categorize_title(title) if title else "Review"
    if category not in dbmod.CATEGORIES:
        category = "Review"

    conn = dbmod.connect()
    dup = _find_duplicate(conn, company, title)
    if dup and not force:
        conn.close()
        print(f"[add_role] soft-blocked: {company!r}/{title!r} matches role {dup['id']}")
        return jsonify({
            "ok": False, "error": "duplicate", "role_id": dup["id"],
            "duplicate": {k: dup[k] for k in ("id", "company", "title", "location", "status")},
            "message": f"Looks like a duplicate of role {dup['id']}: "
                       f"{dup['company']} / {dup['title'] or '(no title)'}.",
        }), 409
    if dup and force:
        print(f"[add_role] force-added past duplicate role {dup['id']}: {company!r}/{title!r}")

    slug = _manual_slug(company, title)
    existing_ids = {r["job_id"] for r in conn.execute("SELECT job_id FROM roles")}
    if slug in existing_ids:
        n = 2
        while f"{slug}-{n}"[:80] in existing_ids:
            n += 1
        slug = f"{slug}-{n}"[:80]

    ts, today = dbmod.now(), dbmod.today()
    cur = conn.execute(
        """INSERT INTO roles (
            job_id, source, company, title, category, url, status,
            jd_text, jd_source, first_seen, last_seen, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (slug, "manual", company, title or None, category, url or None, "sourced",
         jd_text or None, jd_source, today, today, ts, ts),
    )
    conn.commit()
    role_id = cur.lastrowid
    conn.close()
    resp = {"ok": True, "role_id": role_id, "category": category,
            "jd_source": jd_source, "jd_chars": len(jd_text or "")}
    if fetch_preview:
        resp["fetch_preview"] = fetch_preview
    return jsonify(resp)


@app.post("/api/roles/<int:role_id>/fetch_jd")
def api_fetch_jd(role_id):
    """Run the fetch chain against an existing role's url. Returns a preview only
    — never writes to the DB. The client must call .../fetch_jd/accept to store
    it, so a JS-shell page can't reach generation unseen."""
    conn = dbmod.connect()
    row = conn.execute("SELECT id, title, url FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "role not found"}), 404
    url = row["url"]
    if not url:
        return jsonify({"ok": False, "error": "no_url",
                        "message": "No URL on this role. Paste the JD instead."}), 400

    text, _title, source = jd_fetch.fetch_jd(url)
    if text and source in ("fetch", "browser"):
        title_check = jd_fetch.title_words_present(row["title"], text)
        return jsonify({"ok": True, "jd_source": source, "jd_chars": len(text),
                        "preview": text[:500], "text": text,
                        "title_check": title_check,
                        "title_mismatch": title_check["total"] > 0 and title_check["hits"] == 0})
    return jsonify({"ok": False, "error": "fetch_failed", "jd_source": "none",
                    "message": "Couldn't extract a substantial JD from that page. Paste it manually."})


@app.post("/api/roles/<int:role_id>/fetch_jd/accept")
def api_fetch_jd_accept(role_id):
    """Persist a previously previewed fetch. The client echoes back the exact
    text it showed for review — this never re-fetches, so what got reviewed
    is what gets stored, not a second live pull that could differ.

    Second gate here, alongside the char threshold already applied upstream
    in jd_fetch.fetch_jd: title relevance. A real posting page once cleared
    800 chars on pure legal boilerplate with zero of its title's words
    present — recomputed server-side from the role's own stored title
    (never trusts the client), same force=true override idiom /api/roles'
    duplicate check already uses."""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    force = bool(body.get("force"))
    if not text:
        return jsonify({"ok": False, "error": "empty_text"}), 400

    conn = dbmod.connect()
    row = conn.execute("SELECT title FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "role not found"}), 404

    title_check = jd_fetch.title_words_present(row["title"], text)
    title_mismatch = title_check["total"] > 0 and title_check["hits"] == 0
    if title_mismatch and not force:
        conn.close()
        return jsonify({
            "ok": False, "error": "title_mismatch", "title_check": title_check,
            "message": "None of the role title's significant words "
                       f"({', '.join(title_check['significant_words'])}) appear in this "
                       "text — likely not a real JD. Pass force=true to store anyway.",
        }), 409

    cur = conn.execute(
        "UPDATE roles SET jd_text = ?, jd_source = 'fetch', updated_at = ? WHERE id = ?",
        (text, dbmod.now(), role_id),
    )
    conn.commit()
    found = cur.rowcount
    conn.close()
    if not found:
        return jsonify({"ok": False, "error": "role not found"}), 404
    return jsonify({"ok": True, "jd_source": "fetch", "jd_chars": len(text)})


@app.post("/api/roles/<int:role_id>/update")
def api_update(role_id):
    body = request.get_json(silent=True) or {}
    field = body.get("field", "")
    value = body.get("value", "")

    if field not in dbmod.EDITABLE_FIELDS:
        return jsonify({"ok": False, "error": "field not editable"}), 400
    if field == "status" and value not in dbmod.STATUSES:
        return jsonify({"ok": False, "error": "bad status"}), 400
    if field == "category" and value not in dbmod.CATEGORIES:
        return jsonify({"ok": False, "error": "bad category"}), 400
    if field == "interest" and value != "" and value not in dbmod.INTERESTS:
        return jsonify({"ok": False, "error": "bad interest"}), 400
    if field == "decision_reason" and value != "" and value not in dbmod.DECISION_REASONS:
        return jsonify({"ok": False, "error": "bad decision_reason"}), 400

    value = value if value != "" else None
    conn = dbmod.connect()

    # decision_reason is only meaningful on a role NIKO passed on
    # (status='ignored') — a role the EMPLOYER rejected already carries that
    # fact in status itself, so allowing decision_reason there would mix
    # "why I passed" with "why they passed" and stop being a clean filter.
    if field == "decision_reason" and value is not None:
        row = conn.execute("SELECT status FROM roles WHERE id = ?", (role_id,)).fetchone()
        if row and row["status"] != "ignored":
            conn.close()
            return jsonify({"ok": False, "error": "decision_reason_wrong_status",
                            "message": "decision_reason only applies to status='ignored' "
                                       f"(this role is '{row['status']}')."}), 400

    cur = conn.execute(
        f"UPDATE roles SET {field} = ?, updated_at = ? WHERE id = ?",
        (value, dbmod.now(), role_id),
    )
    # Saving JD text by hand marks its provenance as a paste.
    if field == "jd_text":
        conn.execute(
            "UPDATE roles SET jd_source = ? WHERE id = ?",
            ("paste" if value else "none", role_id),
        )
    conn.commit()
    found = cur.rowcount
    conn.close()
    if not found:
        return jsonify({"ok": False, "error": "role not found"}), 404
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Generation + documents
# ------------------------------------------------------------------

def run_fidelity_gates(content_md, *, master_cv_text, master_cv_path, jd_text,
                       profile_text, repairs=(), language_gate=None, tailored_cv_text=None,
                       cross_letter_corpus=None):
    """Every check that stands between a piece of text and documents storage,
    in one place, returning the fidelity dict that becomes critic_notes.
    The caller decides what to do about fidelity["numeric_gate"]["blocked"].

    Shared by generation and by hand-editing rather than copied into each,
    the same way staging.py calls gmail_sweep_apply.py instead of writing a
    second duplicate rule: two copies drift, and then whether a number is
    acceptable starts depending on which door the text came through. A gate
    you can get around by typing the text yourself is not a gate.

    Source-fidelity (advisory, never blocks) records exactly which master CV
    sourced this text — filename plus sha256 — because the master CV is the
    verifier's ground truth and it changes over time. jd_sha256 alongside it
    is JD provenance: a re-fetch overwrites roles.jd_text, so without it,
    which exact JD a stored document was written against is unrecoverable.
    Both are computed from what's on disk NOW, never copied from a parent
    version: if the master CV changed since the draft was generated, that's
    a real fact and the new version should say so.

    The numeric fact gate BLOCKS. Every number must trace back to the master
    CV, the JD, or professional-profile.md. Numbers are discrete — a rephrase
    can't hide an invented one the way prose can — and they're where
    interview liability actually lives.

    This function reports the block; it does not decide what to do about it,
    and the two callers decide differently. Generation climbs a repair ladder
    (rungs 2-4 above) and, failing that, stores the draft as 'blocked'. A
    hand edit is refused outright — the ladder is for model output, not for a
    number someone typed. Both run THIS function, unchanged, on whatever text
    they end up with: a repaired draft passes exactly the gates a fresh one
    does, which is the only reason a repair can be trusted at all.

    language_gate is passed through rather than recomputed. It's a function
    of the JD and declared languages, not of this text, so re-running it on
    an edit would re-decide the role instead of checking the edit; an edited
    version carries its parent's verdict, which is still true of the role.

    The CV experience-order check (cv_order.py) is NOT here, deliberately.
    It needs the master CV's parsed role list and it can mutate
    content_json, which this function never touches — it takes rendered text
    and returns a verdict. The order check runs in the generate route,
    around this call: the master file is checked before generation, and a
    restored order re-enters through here so critic_notes describes the
    document that was actually stored.
    """
    fidelity = verifier.verify_fidelity(content_md, master_cv_text)
    fidelity["master_cv_file"] = Path(master_cv_path).name
    fidelity["master_cv_sha256"] = hashlib.sha256(master_cv_text.encode("utf-8")).hexdigest()
    fidelity["jd_sha256"] = hashlib.sha256((jd_text or "").encode("utf-8")).hexdigest()
    # Advisory only — reports into critic_notes
    # next to verify_fidelity's flags, never blocks. tailored_cv_text (Task 2,
    # implementation pass 2) makes the duplication checks inside
    # run_style_gate live at generation time, not just in the offline
    # baseline scan — the same CV text threaded into the cover-letter
    # generation call itself is what this scores duplication against, so
    # the number reflects what the model actually saw, not a stale
    # fallback.
    fidelity["style_gate"] = style_gate.run_style_gate(content_md, cv_text=tailored_cv_text)
    # Cross-letter phrase repetition (pass 4, Task 4) — advisory, the one
    # check that gets more useful the longer the tool runs, since the
    # corpus it compares against (every other stored cover letter for this
    # profile) only grows. None when the caller has nothing to compare
    # against yet (the CV document, or a role with no other stored letters).
    fidelity["cross_letter_phrases"] = (
        style_gate.scan_new_letter_against_corpus(content_md, cross_letter_corpus)
        if cross_letter_corpus else []
    )
    # Repairs reshape model output that passed the API's own JSON syntax
    # check but not the schema — never apply that invisibly (schema
    # validation passes a well-formed wrong answer). A hand-edit has none.
    fidelity["repairs"] = list(repairs)
    corpus = ng.build_corpus(master_cv_text, jd_text, profile_text)
    fidelity["numeric_gate"] = ng.check_numeric_facts(content_md, corpus)
    if language_gate is not None:
        fidelity["language_gate"] = language_gate
    return fidelity


def cross_letter_corpus(conn, exclude_role_id):
    """Every other stored cover letter for this profile (pass 4, Task 4) —
    every document, not just one per role, so a phrase reused across two
    versions of the SAME other role's letter still counts. Excludes
    exclude_role_id's own documents: comparing a role's letter against its
    own earlier draft isn't a template signal, it's just the same content."""
    rows = conn.execute(
        "SELECT id, content_md FROM documents WHERE doc_type = 'cover_letter' AND role_id != ?",
        (exclude_role_id,),
    ).fetchall()
    return {row["id"]: row["content_md"] for row in rows}


# ---------------------------------------------------------------------------
# The numeric repair ladder
# ---------------------------------------------------------------------------
# One unmatched number used to end the whole generation: a full draft paid
# for, nothing stored, and no record of what the draft said. Regenerating was
# the only recourse and it is a gamble, because a fresh draft rewords
# different numbers — on 29 Sep a retry fixed 700 and broke on 35.
#
# So: try the cheapest fix first, and only climb if it fails.
#   Rung 1  deterministic equivalence, inside numeric_fact_gate. Free.
#   Rung 2  rewrite only the flagged sentences. One small call.
#   Rung 3  one full regeneration, which goes through rungs 1 and 2 again.
#   Rung 4  store the draft as 'blocked' so it can be read and hand-fixed.
#
# The bar never moves. Every rung produces candidate text and nothing more;
# run_fidelity_gates decides whether it is acceptable, on the spliced result,
# exactly as it would for a fresh draft.
NUMERIC_REPAIRS_PER_DRAFT = 1
NUMERIC_REGENERATIONS_PER_DOCUMENT = 1


def _numeric_equivalence_repairs(fidelity):
    """Rung-1 equivalences as entries for critic_notes' repairs list.
    Nothing is fixed invisibly: a number that passed only because "zero" and
    "0" are the same number says so on the document."""
    return [{"kind": "numeric-equivalence", **eq}
            for eq in fidelity["numeric_gate"].get("equivalences", [])]


def _repair_once(gen, fidelity, *, doc_type, role, cfg, profile_text, gate_kwargs, attempts):
    """Rung 2. Returns (gen, fidelity) on a repair that clears every gate,
    or (None, None) — a failed repair leaves the draft exactly as it was.

    A repair that fixes the number but trips something else is a failed
    repair, not a partial win: the draft is re-checked in full, and new
    fidelity flags or gaps count against it. Otherwise "repair" would mean
    "trade a caught problem for an uncaught one"."""
    unmatched = fidelity["numeric_gate"]["unmatched"]
    is_cv_json = doc_type == "cv" and gen.get("content_json")
    spans = (numeric_repair.flagged_json_fields(gen["content_json"], unmatched)
             if is_cv_json
             else numeric_repair.flagged_sentences(gen["content_md"], unmatched))
    if not spans:
        # The numbers are somewhere no span selector reached. Nothing to
        # send, so nothing to repair — climb.
        attempts.append({"rung": 2, "action": "repair", "outcome": "no_spans",
                         "unmatched": unmatched})
        return None, None

    lines = numeric_repair.source_lines(gen["master_cv_text"], role["jd_text"], profile_text)
    system = pa.build_system_prompt(cfg, gen["master_cv_text"], category=role.get("category"))
    try:
        rewrites, usage, cost = generation.repair_numbers(
            spans, lines, unmatched, doc_type, cfg["model"], system)
    except Exception as exc:  # noqa: BLE001 — a failed repair is a rung, not a 500
        attempts.append({"rung": 2, "action": "repair", "outcome": "error", "error": str(exc)})
        return None, None

    attempt = {"rung": 2, "action": "repair", "unmatched": unmatched,
               "spans": len(spans), "usage": usage, "cost_usd": cost}
    attempts.append(attempt)
    if not rewrites:
        attempt["outcome"] = "no_rewrites"
        return None, None

    candidate = dict(gen)
    if is_cv_json:
        patched = numeric_repair.splice_json_fields(gen["content_json"], spans, rewrites)
        candidate["content_json"] = patched
        # The markdown is DERIVED from the JSON. Patch the JSON and
        # re-render; never patch the render, which the next render would
        # overwrite and which would leave content_json disagreeing with the
        # document it supposedly describes.
        candidate["content_md"] = cv_schema.cv_json_to_markdown(patched)
    else:
        candidate["content_md"] = numeric_repair.splice_sentences(
            gen["content_md"], spans, rewrites)

    candidate_fidelity = run_fidelity_gates(candidate["content_md"], **gate_kwargs)
    if candidate_fidelity["numeric_gate"]["blocked"]:
        attempt["outcome"] = "still_unmatched"
        attempt["remaining"] = candidate_fidelity["numeric_gate"]["unmatched"]
        return None, None
    if (len(candidate_fidelity["flags"]) > len(fidelity["flags"])
            or len(candidate_fidelity["gaps"]) > len(fidelity["gaps"])):
        attempt["outcome"] = "broke_another_gate"
        return None, None

    attempt["outcome"] = "repaired"
    by_id = {r["id"]: r["text"] for r in rewrites}
    candidate_fidelity["repairs"] = list(fidelity.get("repairs", [])) + [
        {"kind": "numeric-rewrite", "before": span["text"], "after": by_id[span["id"]]}
        for span in spans if span["id"] in by_id and by_id[span["id"]].strip() != span["text"].strip()
    ]
    return candidate, candidate_fidelity


def _generate_or_error(role, doc_type, cfg, tailored_cv_text):
    """One draft, or the HTTP error it failed with. Returns (gen, None) or
    (None, (response, code)).

    Factored out because rung 3 regenerates, and a regeneration has to
    handle an out-of-credits or refused call exactly as the first draft
    does rather than falling through into the ladder with no draft."""
    try:
        return generation.generate(role, doc_type, cfg,
                                   tailored_cv_text=tailored_cv_text), None
    except pa.AssemblyError as e:
        return None, (jsonify({"ok": False, "error": "assembly", "message": str(e)}), 400)
    except Exception as e:  # noqa: BLE001 — surface the API error to the UI
        msg = str(e)
        # Anthropic doesn't raise a distinct exception type for this —
        # it's a BadRequestError like any other 400, but always carries
        # this exact phrase (documented API behavior, not inferred), so
        # it's the only reliable way to tell "out of credits" apart from
        # every other reason a request can fail. Worth a clear, specific
        # message: the generic "generation_failed" 502 buried the real
        # cause behind an SDK exception string a user has no reason to
        # recognize as "go add credits," not e.g. a JD/prompt problem.
        if "credit balance" in msg.lower():
            return None, (jsonify({
                "ok": False, "error": "insufficient_credits",
                "message": "Your Anthropic API credit balance is too low to generate. "
                           "Top up at console.anthropic.com/settings/billing, then try again.",
            }), 402)
        return None, (jsonify({"ok": False, "error": "generation_failed", "message": msg}), 502)


def numeric_block_payload(numeric, doc_type):
    """The refusal, worded once.

    Now used only by the edit path. Generation stopped returning this in
    2026-09-29's repair-ladder pass: a blocked draft is stored as 'blocked'
    and reported in the normal documents payload, because discarding it was
    what left no record of what a blocked draft actually said.

    It stays shared rather than being inlined into the edit route, because
    the wording is the part that has to agree. Both paths enforce one rule
    and must teach one lesson about it — only the recovery differs, and the
    edit path deliberately has none."""
    return {
        "ok": False, "error": "numeric_fact_gate",
        "message": "Blocked: number(s) in the draft don't trace back to the "
                   "master CV, the JD, or professional-profile.md: "
                   + ", ".join(numeric["unmatched"]),
        "unmatched": numeric["unmatched"],
        "doc_type": doc_type,
    }


@app.post("/api/roles/<int:role_id>/generate")
def api_generate(role_id):
    body = request.get_json(silent=True) or {}
    doc_types = body.get("doc_types") or ["cv", "cover_letter"]

    conn = dbmod.connect()
    row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "role not found"}), 404
    role = dict(row)

    # Guardrails (spec section 7): need a JD, and a category that maps to a CV.
    if not (role.get("jd_text") or "").strip():
        conn.close()
        return jsonify({"ok": False, "error": "no_jd",
                        "message": "Paste the job description before generating."}), 400
    cfg = pa.load_config()
    cv_map = cfg.get("category_cv_map", {})
    if not cv_map.get(role.get("category")):
        conn.close()
        return jsonify({"ok": False, "error": "no_category",
                        "message": "Pick a category (Leadership / Advisory / Senior IC) "
                                   "before generating."}), 400

    # The master CV's own timeline has to be right before anything is
    # generated from it. Checked HERE, before the API call, because a draft
    # written against an out-of-order master inherits the order and would
    # cost tokens to produce something that has to be thrown away. The fix
    # belongs in the one source file, not in every draft.
    try:
        master_cv_path, master_cv_text = pa.resolve_master_cv(role.get("category"), cfg)
        cv_order.check_master_order(master_cv_text, Path(master_cv_path).name)
    except pa.AssemblyError as e:
        conn.close()
        return jsonify({"ok": False, "error": "no_category", "message": str(e)}), 400
    except cv_order.MasterOrderError as e:
        conn.close()
        print(f"[generate] role {role_id} BLOCKED by master CV order: {e}")
        return jsonify({"ok": False, "error": "master_cv_order",
                        "message": str(e)}), 422

    # professional-profile.md is part of the numeric-fact corpus (see below) —
    # loaded once per request, not per doc_type, since it doesn't change.
    profile_text = Path(cfg["paths"]["about_me"]).read_text(encoding="utf-8")

    # Language gate (Swiss-specific), enforcing. Runs once per role, not per
    # doc_type, since it only depends on jd_text. A BLOCK stops generation
    # before any API call — the caller must either decline the role
    # (POST .../language_gate/decline, which closes the loop via
    # language_gate.decline_role so the scanner never resurfaces it) or
    # re-request with override_language_gate=true to proceed knowingly.
    # WARN never blocks — it's recorded in critic_notes and flows into the
    # draft the same way an unverifiable claim already becomes a [GAP].
    declared_languages = cfg.get("profile", {}).get("languages", {})
    lang_gate_result = language_gate.classify_language_gate(role["jd_text"], declared_languages)
    lang_gate_verdict = ("block" if lang_gate_result["block"]
                         else "warn" if lang_gate_result["warn"] else "clear")
    override_language_gate = bool(body.get("override_language_gate"))
    lang_gate_overridden = bool(lang_gate_result["block"]) and override_language_gate

    if lang_gate_result["block"] and not override_language_gate:
        conn.close()
        print(f"[generate] role {role_id} BLOCKED by language gate: "
              f"{language_gate.format_block_message(lang_gate_result['block'])}")
        return jsonify({
            "ok": False, "error": "language_gate_block",
            "message": language_gate.format_block_message(lang_gate_result["block"]),
            "block": lang_gate_result["block"],
        }), 422

    # CV-awareness for the cover letter call: the current request's own CV result
    # takes precedence over a stored one (fresher, and reflects whatever
    # this exact request just tailored), falling back to the latest stored
    # CV for this role if "cv" isn't part of this request, and to None if
    # neither exists — generate() ignores this entirely for doc_type "cv".
    cv_row = conn.execute(
        # status != 'blocked': a CV whose numbers never cleared the gate is
        # not a CV a cover letter may build on. It is stored so it can be
        # read and fixed, not so it can be used.
        "SELECT content_md FROM documents WHERE role_id = ? AND doc_type = 'cv' "
        "AND status != 'blocked' ORDER BY version DESC LIMIT 1", (role_id,),
    ).fetchone()
    tailored_cv_text = cv_row["content_md"] if cv_row else None

    results = []
    for dt in doc_types:
        attempts = []
        gen, err = _generate_or_error(role, dt, cfg, tailored_cv_text)
        if err:
            conn.close()
            return err
        attempts.append({"rung": 1, "action": "generate",
                         "usage": gen["usage"], "cost_usd": gen["cost_usd"]})
        cv_text_before = tailored_cv_text
        if dt == "cv":
            # A CV generated earlier in this same request is fresher than
            # whatever was already stored — use it for any cover_letter
            # still to come in this loop.
            tailored_cv_text = gen["content_md"]

        def _gate_kwargs(g):
            return dict(
                master_cv_text=g["master_cv_text"], master_cv_path=g["master_cv_path"],
                jd_text=role["jd_text"], profile_text=profile_text,
                repairs=g["repairs"],
                language_gate={"verdict": lang_gate_verdict,
                               "overridden": lang_gate_overridden, **lang_gate_result},
                # Duplication-vs-CV is meaningless for the CV document itself —
                # only score it for the cover letter.
                tailored_cv_text=tailored_cv_text if dt == "cover_letter" else None,
                cross_letter_corpus=(cross_letter_corpus(conn, role_id)
                                     if dt == "cover_letter" else None),
            )

        gate_kwargs = _gate_kwargs(gen)
        fidelity = run_fidelity_gates(gen["content_md"], **gate_kwargs)
        fidelity["repairs"] += _numeric_equivalence_repairs(fidelity)

        # Rung 2 — repair only the flagged sentences.
        if fidelity["numeric_gate"]["blocked"]:
            fixed, fixed_fidelity = _repair_once(
                gen, fidelity, doc_type=dt, role=role, cfg=cfg,
                profile_text=profile_text, gate_kwargs=gate_kwargs, attempts=attempts)
            if fixed:
                gen, fidelity = fixed, fixed_fidelity
                if dt == "cv":
                    tailored_cv_text = gen["content_md"]

        # Rung 3 — one full regeneration, which gets rungs 1 and 2 again.
        # It does not recurse: there is no second regeneration.
        if fidelity["numeric_gate"]["blocked"]:
            regen, regen_err = _generate_or_error(role, dt, cfg, cv_text_before)
            if regen_err is None:
                attempts.append({"rung": 3, "action": "regenerate",
                                 "usage": regen["usage"], "cost_usd": regen["cost_usd"]})
                gen = regen
                if dt == "cv":
                    tailored_cv_text = gen["content_md"]
                gate_kwargs = _gate_kwargs(gen)
                fidelity = run_fidelity_gates(gen["content_md"], **gate_kwargs)
                fidelity["repairs"] += _numeric_equivalence_repairs(fidelity)
                if fidelity["numeric_gate"]["blocked"]:
                    fixed, fixed_fidelity = _repair_once(
                        gen, fidelity, doc_type=dt, role=role, cfg=cfg,
                        profile_text=profile_text, gate_kwargs=gate_kwargs,
                        attempts=attempts)
                    if fixed:
                        gen, fidelity = fixed, fixed_fidelity
                        if dt == "cv":
                            tailored_cv_text = gen["content_md"]

        # Experience order. Deterministic, no API call: the master CV's role
        # order is the CV's timeline, and the model does not get a vote on it.
        if dt == "cv" and gen.get("content_json"):
            master_roles = cv_order.parse_master_roles(gen["master_cv_text"])
            verdict = cv_order.compare_experience_order(
                gen["content_json"].get("experience"), master_roles)
            if verdict["status"] == "reordered":
                # Mechanical, so fix it in code. Regenerating would pay a
                # model to redo something a sort already did correctly.
                gen["content_json"]["experience"] = cv_order.restore_order(
                    gen["content_json"]["experience"], master_roles)
                gen["content_md"] = cv_schema.cv_json_to_markdown(gen["content_json"])
                if dt == "cv":
                    tailored_cv_text = gen["content_md"]
                # Re-gate the text that will actually be stored. Permuting
                # blocks cannot change any gate's verdict (every check here
                # is over a set or is order-independent), so this is belt
                # rather than braces — but critic_notes has to describe the
                # document that got stored, not the one before the sort.
                carried = list(fidelity["repairs"])
                fidelity = run_fidelity_gates(gen["content_md"], **_gate_kwargs(gen))
                fidelity["repairs"] = carried + [{
                    "kind": "experience-order",
                    "restored": ", ".join(verdict["order"]),
                }]
                print(f"[generate] role {role_id} cv: experience order restored "
                      f"to master CV order")
            elif verdict["status"] == "mismatch":
                # NOT fixed. Which roles belong in this CV is a fidelity
                # question, and reordering whatever came back would hide it.
                # Recorded at the entity check's severity — advisory, in the
                # same flags list, because it is the same kind of claim: the
                # draft says something the master CV does not.
                parts = []
                if verdict["added"]:
                    parts.append("not in the master CV: " + ", ".join(verdict["added"]))
                if verdict["dropped"]:
                    parts.append("missing from the draft: " + ", ".join(verdict["dropped"]))
                fidelity["flags"].append({
                    "kind": "experience-set",
                    "text": "; ".join(parts) or "duplicate roles in the draft",
                })
                print(f"[generate] role {role_id} cv: experience SET differs from "
                      f"the master CV — flagged, not reordered")

        # Rung 4 — stop, but keep the draft. Discarding it was why there was
        # never any record of what a blocked draft actually said.
        numeric = fidelity["numeric_gate"]
        status = "blocked" if numeric["blocked"] else "draft"
        if numeric["blocked"]:
            attempts.append({"rung": 4, "action": "store_blocked",
                             "unmatched": numeric["unmatched"]})
            print(f"[generate] role {role_id} {dt} STORED BLOCKED after "
                  f"{len(attempts)} attempt(s): {numeric['unmatched']}")
            if dt == "cv":
                # A blocked CV is not a CV. Anything later in this request
                # falls back to what was there before it.
                tailored_cv_text = cv_text_before
        fidelity["numeric_attempts"] = attempts

        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM documents "
            "WHERE role_id = ? AND doc_type = ?", (role_id, dt),
        ).fetchone()[0]

        # Commitment coverage is computed inside generation, where the plan
        # exists; it rides into critic_notes with the other advisory checks
        # rather than being recomputed here, which couldn't be done anyway
        # without the plan. None on the freeform path, and absent from
        # critic_notes entirely in that case rather than stored as a
        # misleading empty pass.
        if gen.get("commitment_coverage") is not None:
            fidelity["commitment_coverage"] = gen["commitment_coverage"]
            fidelity["commitment_retried"] = gen.get("coverage_retried", False)

        critic_notes = json.dumps(fidelity)
        content_json = json.dumps(gen["content_json"]) if gen["content_json"] is not None else None

        cur = conn.execute(
            """INSERT INTO documents (
                role_id, doc_type, version, format, content_md, content_json,
                model, critic_notes, status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (role_id, dt, version, "md", gen["content_md"], content_json,
             gen["model"], critic_notes, status, dbmod.now()),
        )
        # Commit per document, not once at the end of the doc_types loop. A
        # later doc type in the same request can still fail (generation error,
        # numeric-gate block) and return early — that must not roll back a
        # document that already generated and cleared its own gates. Before
        # this, a cover-letter failure silently discarded a CV inserted
        # moments earlier in the same request, because sqlite3 rolls back
        # whatever wasn't committed when the connection closes.
        conn.commit()
        u = gen["usage"]
        print(f"[generate] role {role_id} {dt} v{version} | "
              f"in={u['input_tokens']} out={u['output_tokens']} "
              f"cache_r={u['cache_read']} cache_w={u['cache_write']} | "
              f"${gen['cost_usd']} | {gen['model']} | stop={gen['stop_reason']} | "
              f"retried={gen['retried']} | repairs={len(gen['repairs'])} | "
              f"fidelity_flags={len(fidelity['flags'])} gaps={len(fidelity['gaps'])} | "
              f"language_gate={lang_gate_verdict}" + (" (overridden)" if lang_gate_overridden else ""))
        results.append({
            "id": cur.lastrowid, "doc_type": dt, "version": version,
            "model": gen["model"], "cost_usd": gen["cost_usd"],
            "usage": u, "truncated": gen["truncated"], "retried": gen["retried"],
            "fidelity_flags": len(fidelity["flags"]), "gaps": len(fidelity["gaps"]),
            "repairs": len(fidelity["repairs"]), "language_gate_verdict": lang_gate_verdict,
            "language_gate_overridden": lang_gate_overridden,
            "status": status, "unmatched_numbers": numeric["unmatched"],
            "numeric_attempts": len(attempts),
        })

    conn.close()
    return jsonify({"ok": True, "documents": results})


@app.get("/api/roles/<int:role_id>/documents")
def api_documents(role_id):
    conn = dbmod.connect()
    rows = conn.execute(
        "SELECT id, doc_type, version, format, model, status, edited_from, "
        "created_at, critic_notes "
        "FROM documents WHERE role_id = ? ORDER BY doc_type, version DESC",
        (role_id,),
    ).fetchall()
    conn.close()
    docs = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k != "critic_notes"}
        fidelity = json.loads(r["critic_notes"]) if r["critic_notes"] else None
        d["fidelity_flags"] = len(fidelity["flags"]) if fidelity else 0
        d["gaps"] = len(fidelity["gaps"]) if fidelity else 0
        lg = fidelity.get("language_gate") if fidelity else None
        d["language_gate_verdict"] = lg["verdict"] if lg else None
        d["language_gate_overridden"] = bool(lg.get("overridden")) if lg else False
        docs.append(d)
    return jsonify({"documents": docs})


@app.post("/api/roles/<int:role_id>/language_gate/decline")
def api_language_gate_decline(role_id):
    """Close the loop on a BLOCK: recomputes the verdict server-side (never
    trusts client-supplied evidence) and, if it still blocks, writes
    status='ignored' + decision_reason='language' + the requirement quoted
    verbatim via language_gate.decline_role() — this is what stops the
    scanner resurfacing the role."""
    conn = dbmod.connect()
    row = conn.execute("SELECT jd_text FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "role not found"}), 404

    cfg = pa.load_config()
    declared_languages = cfg.get("profile", {}).get("languages", {})
    result = language_gate.classify_language_gate(row["jd_text"] or "", declared_languages)
    if not result["block"]:
        conn.close()
        return jsonify({"ok": False, "error": "no_block",
                        "message": "Language gate doesn't block this role — nothing to decline."}), 400

    language_gate.decline_role(conn, role_id, result["block"])
    updated = conn.execute(
        "SELECT status, decision_reason, decision_note FROM roles WHERE id = ?", (role_id,)
    ).fetchone()
    conn.close()
    return jsonify({"ok": True, "status": updated["status"],
                    "decision_reason": updated["decision_reason"],
                    "decision_note": updated["decision_note"]})


@app.get("/api/documents/<int:doc_id>")
def api_document(doc_id):
    conn = dbmod.connect()
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.post("/api/documents/<int:doc_id>/edit")
def api_document_edit(doc_id):
    """Hand-edit a cover letter (BUILD-SPEC section 1: "draft off -> edit,
    nothing is a dead end"). Until now the only fix for one wrong sentence
    was regenerating the whole document.

    Cover letters only. A CV is structured content_json that the renderer
    and the PDF depend on, and free-text editing would break the schema
    they read — a different feature, deliberately not this one.

    The gates re-run against the edited text, through the same
    run_fidelity_gates() the generate route uses. A number typed by hand
    gets exactly the treatment a number written by the model gets, and a
    blocked edit stores nothing.

    The result is a NEW version. The parent row is never updated: it is the
    record of what the model actually produced, and losing that would make
    "did I write this or did it" unanswerable a week later.
    """
    body = request.get_json(silent=True) or {}
    content_md = (body.get("content_md") or "").strip()

    conn = dbmod.connect()
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"ok": False, "error": "not_found"}), 404
    if doc["doc_type"] != "cover_letter":
        conn.close()
        return jsonify({"ok": False, "error": "not_editable",
                        "message": "Only cover letters can be edited. A CV is structured "
                                   "content the renderer depends on — regenerate it "
                                   "instead."}), 400
    if not content_md:
        conn.close()
        return jsonify({"ok": False, "error": "empty",
                        "message": "An edited cover letter can't be empty."}), 400
    if content_md == (doc["content_md"] or "").strip():
        conn.close()
        return jsonify({"ok": False, "error": "unchanged",
                        "message": "No changes to save."}), 400

    role = conn.execute("SELECT * FROM roles WHERE id = ?", (doc["role_id"],)).fetchone()
    if not role:
        conn.close()
        return jsonify({"ok": False, "error": "role_not_found"}), 404
    if not (role["jd_text"] or "").strip():
        conn.close()
        return jsonify({"ok": False, "error": "no_jd",
                        "message": "The role has no job description, so the numeric gate "
                                   "has nothing to check against."}), 400

    # Re-derived from the role and config at save time, never copied from the
    # parent version — see run_fidelity_gates on why that distinction matters.
    cfg = pa.load_config()
    try:
        master_cv_path, master_cv_text = pa.resolve_master_cv(role["category"], cfg)
    except pa.AssemblyError as e:
        conn.close()
        return jsonify({"ok": False, "error": "no_category", "message": str(e)}), 400
    profile_text = Path(cfg["paths"]["about_me"]).read_text(encoding="utf-8")

    parent_fidelity = json.loads(doc["critic_notes"]) if doc["critic_notes"] else {}
    cv_row = conn.execute(
        "SELECT content_md FROM documents WHERE role_id = ? AND doc_type = 'cv' "
        "AND status != 'blocked' ORDER BY version DESC LIMIT 1", (doc["role_id"],),
    ).fetchone()
    fidelity = run_fidelity_gates(
        content_md,
        master_cv_text=master_cv_text, master_cv_path=master_cv_path,
        jd_text=role["jd_text"], profile_text=profile_text,
        language_gate=parent_fidelity.get("language_gate"),
        tailored_cv_text=cv_row["content_md"] if cv_row else None,
        # doc["doc_type"] is always "cover_letter" here, checked earlier in
        # this route, so this is never wasted for a CV edit.
        cross_letter_corpus=cross_letter_corpus(conn, doc["role_id"]),
    )
    numeric = fidelity["numeric_gate"]
    if numeric["blocked"]:
        conn.close()
        print(f"[edit] document {doc_id} BLOCKED by numeric fact gate: {numeric['unmatched']}")
        return jsonify(numeric_block_payload(numeric, doc["doc_type"])), 422

    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM documents "
        "WHERE role_id = ? AND doc_type = ?", (doc["role_id"], doc["doc_type"]),
    ).fetchone()[0]

    # model stays NULL: it records which model produced the text, and no
    # model produced this one. status starts at 'draft' however the parent
    # ended up — approval is of a specific text, and this is different text.
    cur = conn.execute(
        """INSERT INTO documents (
            role_id, doc_type, version, format, content_md, content_json,
            model, critic_notes, status, edited_from, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (doc["role_id"], doc["doc_type"], version, "md", content_md, None,
         None, json.dumps(fidelity), "draft", doc_id, dbmod.now()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    print(f"[edit] role {doc['role_id']} cover_letter v{version} hand-edited from "
          f"document {doc_id} | flags={len(fidelity['flags'])} gaps={len(fidelity['gaps'])} "
          f"| numbers checked={numeric['checked']}")
    return jsonify({"ok": True, "id": new_id, "version": version,
                    "edited_from": doc_id,
                    "fidelity_flags": len(fidelity["flags"]),
                    "gaps": len(fidelity["gaps"])})


@app.get("/api/documents/<int:doc_id>/preview")
def api_document_preview(doc_id):
    """Render a CV's content_json to HTML (spec section 11 / Phase 3b). Reads
    content_json only — never content_md — so this is pure presentation over
    the same structured content the future PDF export will use."""
    conn = dbmod.connect()
    row = conn.execute(
        "SELECT d.content_json, d.doc_type, r.category "
        "FROM documents d JOIN roles r ON r.id = d.role_id WHERE d.id = ?",
        (doc_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    if row["doc_type"] != "cv" or not row["content_json"]:
        return jsonify({"ok": False, "error": "no_preview",
                        "message": "Preview is only available for CVs with structured content."}), 400
    cfg = pa.load_config()
    try:
        html = cv_render.render_cv_html(json.loads(row["content_json"]), row["category"], cfg)
    except cv_render.RenderError as e:
        return jsonify({"ok": False, "error": "render_failed", "message": str(e)}), 500
    return Response(html, mimetype="text/html")


def _pdf_slug(*parts):
    joined = "-".join(parts)
    return re.sub(r"[^a-z0-9]+", "-", joined.lower()).strip("-")[:120] or "role"


def _pdf_output_path(company, title, doc_type, version):
    return APPLICATIONS_DIR / _pdf_slug(company, title) / f"{doc_type}-v{version}.pdf"


@app.get("/api/documents/<int:doc_id>/download")
def api_document_download(doc_id):
    """Serve a document's PDF (spec section 6), generating it on demand the
    first time — never during /generate, so drafting stays fast. CVs render
    from content_json; cover letters get the optional letterhead PDF, built
    from content_md on the same request that asks for it. Text stays the
    cover letter's default and only automatic output."""
    conn = dbmod.connect()
    row = conn.execute(
        "SELECT d.*, r.category, r.company, r.title, r.jd_text FROM documents d "
        "JOIN roles r ON r.id = d.role_id WHERE d.id = ?", (doc_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404
    if row["status"] == "blocked":
        # Rung 4 keeps a blocked draft so it can be read and hand-fixed. A
        # PDF is the one thing that leaves this machine, so it is the one
        # thing a draft with unsourced numbers must never become.
        conn.close()
        return jsonify({
            "ok": False, "error": "blocked_document",
            "message": "This draft is blocked: number(s) in it don't trace back to "
                       "the master CV, the JD, or professional-profile.md. Fix them "
                       "in the editor first.",
        }), 409

    existing = row["file_path"]
    if existing and Path(existing).exists():
        conn.close()
        return send_file(existing, as_attachment=True, download_name=Path(existing).name)

    cfg = pa.load_config()
    try:
        if row["doc_type"] == "cv":
            if not row["content_json"]:
                conn.close()
                return jsonify({"ok": False, "error": "no_content_json", "message":
                                "This CV predates structured content. Regenerate it first."}), 400
            html = cv_render.render_cv_html(json.loads(row["content_json"]), row["category"], cfg)
        else:
            if not row["content_md"]:
                conn.close()
                return jsonify({"ok": False, "error": "no_content", "message": "Nothing to render."}), 400
            html = cv_render.render_cover_letter_html(row["content_md"], row["category"], cfg)
        out_path = _pdf_output_path(row["company"], row["title"], row["doc_type"], row["version"])
        pdf_export.html_to_pdf(html, out_path)
    except (cv_render.RenderError, pdf_export.PdfExportError) as e:
        conn.close()
        return jsonify({"ok": False, "error": "pdf_export_failed", "message": str(e)}), 500

    # ATS text-layer gate — hard, after every render. A CV that looks perfect
    # on screen can still fail an ATS if the PDF's text layer doesn't carry
    # what the eye sees (icon-only contact glyphs, scrambled reading order
    # from absolutely-positioned blocks, font-substitution garbage). Checked
    # against the actual extracted text an ATS parser gets, not the pixels.
    try:
        gate = ats_verify.verify_ats_text_layer(
            out_path,
            email=cfg.get("profile", {}).get("email"),
            phone=cfg.get("profile", {}).get("phone"),
            jd_text=row["jd_text"],
        )
    except ats_verify.PdftotextNotFound as e:
        conn.close()
        return jsonify({"ok": False, "error": "ats_gate_unavailable", "message": str(e)}), 500
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        conn.close()
        return jsonify({"ok": False, "error": "ats_gate_timeout",
                         "message": "ATS text-layer check timed out."}), 500

    # posting_keywords' "stuffed" signal is deliberately NOT part of the hard
    # gate below — live-tested against real generated CVs and found to fire
    # on ordinary domain vocabulary, not stuffing: a design CV legitimately
    # says "design" 19-35x across two pages against a JD's 2-9 mentions, the
    # same shape a rendering-bug duplication would have by this heuristic
    # alone. Word-frequency ratio can't tell dense professional writing from
    # actual stuffing apart; still computed and returned for the record
    # (retroactive reports, future human review), just not blocking.
    problems = (
        gate["cid_or_replacement_chars"] + gate["contact_literal_text"]
        + gate["reading_order"] + gate["internal_repetition"]
    )
    if problems:
        out_path.unlink(missing_ok=True)  # never leave a failed render on disk to be served later
        conn.close()
        all_problems = problems
        print(f"[download] doc {doc_id} BLOCKED by ATS text-layer gate: {all_problems}")
        return jsonify({
            "ok": False, "error": "ats_gate_failed",
            "message": "Blocked: this PDF's text layer doesn't reliably carry what's "
                        "rendered, which an ATS parser would see as broken or missing content: "
                        + "; ".join(all_problems),
            "problems": all_problems,
        }), 422

    conn.execute("UPDATE documents SET file_path = ?, format = 'pdf' WHERE id = ?",
                 (str(out_path), doc_id))
    conn.commit()
    conn.close()
    return send_file(out_path, as_attachment=True, download_name=out_path.name)


@app.post("/api/documents/<int:doc_id>/approve")
def api_approve(doc_id):
    conn = dbmod.connect()
    row = conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404
    if row["status"] == "blocked":
        # Approving a blocked draft would make it exportable through the
        # front door, which is the whole thing rung 4 exists to prevent.
        # Said as its own refusal rather than a 404, which would claim the
        # document isn't there when it demonstrably is.
        conn.close()
        return jsonify({
            "ok": False, "error": "blocked_document",
            "message": "This draft is blocked on unsourced numbers. Fix them in the "
                       "editor first — the edit path re-runs the same gate.",
        }), 409
    conn.execute("UPDATE documents SET status = 'approved' WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Scan (in-process) -> upsert into SQLite
# ------------------------------------------------------------------

def _ignored_and_rejected_lookup(conn):
    """(normalized company, normalized title) -> id of the EARLIEST row in
    that repost chain, for every role currently 'ignored' or 'rejected'.
    Built once per scan rather than queried per match — this is the same
    normalization _find_duplicate() uses, so a repost matches by the same
    rule a manual duplicate would."""
    lookup = {}
    for r in conn.execute(
        "SELECT id, company, title, repost_of FROM roles "
        "WHERE status IN ('ignored','rejected') AND merged_into IS NULL"
    ):
        key = (_norm(r["company"]), _norm(r["title"]))
        lookup[key] = r["repost_of"] or r["id"]
    return lookup


def scan_into_db(all_locations=False, trigger="manual"):
    """Run scan_all_companies_detailed() and upsert results into roles.
    Mirrors the CLI diff logic (new / still-live / expired) but writes to
    SQLite. Only auto-sourced rows are ever auto-expired, so curated
    pipeline rows are safe.

    A new posting whose normalized company+title matches a role already
    ignored or rejected is still inserted (it may genuinely need a fresh
    decision) but gets repost_of set to that earlier row's chain root, so
    the UI can surface the prior outcome instead of presenting it as brand
    new — the whole point being you shouldn't have to re-decide the same
    role every scan without at least seeing what you decided last time.

    Also records one scan_runs + N scan_run_companies rows (scan
    observability) — the funnel (raw/title/location/inserted) per company,
    every dropped title/location capped at 50, and the outcome/error per
    company. Recorded even when nothing landed, since a clean zero-match
    company (title_matches/location_matches empty, outcome='ok') is a
    different, more useful signal than an error would be."""
    dbmod.backup_db("scan")
    started_at = dbmod.now()
    detailed = scan_all_companies_detailed()
    for c in detailed:
        for m in c["title_matches"]:
            m["category"] = categorize_title(m["title"])
            # location_matches shares the same dict objects as title_matches
            # (job_scanner builds it as a filter over title_matches, not a
            # copy), so tagging title_matches here also tags location_matches.

    raw_matches = [m for c in detailed for m in c["title_matches"]]
    matches = raw_matches if all_locations else [m for c in detailed for m in c["location_matches"]]
    current_ids = {m["job_id"] for m in raw_matches if m.get("job_id")}
    # A company that scanned cleanly but matched nothing (a real honest
    # zero — seen in practice on real Oracle HCM providers) is still 'ok',
    # not absent from this set — the old version derived "scanned" from
    # raw_matches containing at least one posting, which meant a clean
    # zero-match company could never have its own stale rows auto-expired.
    # Fixed here, not a new behavior.
    scanned_companies = {c["company"] for c in detailed if c["outcome"] == "ok"}
    error_companies = {c["company"] for c in detailed if c["outcome"] != "ok"}
    today = dbmod.today()
    ts = dbmod.now()

    conn = dbmod.connect()
    existing_ids = {r["job_id"] for r in conn.execute("SELECT job_id FROM roles")}
    decided_lookup = _ignored_and_rejected_lookup(conn)
    new_count = 0
    repost_count = 0
    inserted_by_company = defaultdict(int)

    for m in matches:
        jid = m.get("job_id")
        if not jid:
            continue
        if jid in existing_ids:
            conn.execute(
                "UPDATE roles SET last_seen = ?, updated_at = ? WHERE job_id = ?",
                (today, ts, jid),
            )
        else:
            repost_of = decided_lookup.get((_norm(m.get("company")), _norm(m.get("title"))))
            if repost_of:
                repost_count += 1
            conn.execute(
                """INSERT INTO roles (
                    job_id, source, company, title, category, location, url,
                    portal, posted, status, jd_source,
                    first_seen, last_seen, created_at, updated_at, repost_of
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    jid, "auto", m.get("company"), m.get("title"),
                    m.get("category"), m.get("location"), m.get("url"),
                    m.get("portal"), m.get("posted"), "sourced", "none",
                    today, today, ts, ts, repost_of,
                ),
            )
            existing_ids.add(jid)
            new_count += 1
            inserted_by_company[m.get("company")] += 1

    # Expire auto-sourced rows whose company scanned cleanly but no longer lists them.
    live_companies = scanned_companies - error_companies
    expired = 0
    candidates = conn.execute(
        "SELECT job_id, company FROM roles WHERE source = 'auto' AND status = 'sourced'"
    ).fetchall()
    for r in candidates:
        if r["company"] in live_companies and r["job_id"] not in current_ids:
            conn.execute(
                "UPDATE roles SET status = 'expired', notes = COALESCE(notes,'') "
                "|| ? , updated_at = ? WHERE job_id = ?",
                (f" [listing gone as of {today}]", ts, r["job_id"]),
            )
            expired += 1

    finished_at = dbmod.now()
    for c in detailed:
        c["inserted"] = inserted_by_company.get(c["company"], 0)
    dbmod.record_scan_run(conn, trigger, started_at, finished_at, detailed, new_count, expired)

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()

    errors = [(c["company"], c["portal"], c["error_detail"]) for c in detailed if c["outcome"] != "ok"]
    lines = [f"Scan complete. {len(matches)} live matches, "
             f"{new_count} new ({repost_count} of them reposts of a decided role), "
             f"{expired} newly expired, {len(errors)} API errors."]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for company, handler, msg in errors:
            lines.append(f"  {company} ({handler}): {msg}")
    lines.append("")
    lines.append(f"{total} roles in the cockpit.")
    return {
        "ok": len(errors) < 999,
        "new": new_count,
        "reposts": repost_count,
        "expired": expired,
        "errors": len(errors),
        "output": "\n".join(lines),
    }


@app.post("/api/scan")
def api_scan():
    result = scan_into_db()
    return jsonify(result)


@app.get("/api/scan_runs/latest")
def api_scan_runs_latest():
    """Scan observability: the two questions the UI answers are "did the
    scan work" (the run summary) and "why did nothing land" (per-company
    funnel + the actual dropped titles/locations, not just counts).
    Only the latest run — no history browser, deliberately out of scope."""
    conn = dbmod.connect()
    run = conn.execute("SELECT * FROM scan_runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    if not run:
        conn.close()
        return jsonify({"run": None, "companies": []})
    rows = conn.execute(
        "SELECT * FROM scan_run_companies WHERE run_id = ? ORDER BY company", (run["id"],)
    ).fetchall()
    companies = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["dropped_titles"] = json.loads(d["dropped_titles"] or "[]")
        d["dropped_locations"] = json.loads(d["dropped_locations"] or "[]")
        d["failing_since"] = dbmod.failing_since(conn, r["company"]) if r["outcome"] == "error" else None
        companies.append(d)
    conn.close()
    return jsonify({"run": {k: run[k] for k in run.keys()}, "companies": companies})


# ------------------------------------------------------------------
# Staged finds (see staging.py)
# ------------------------------------------------------------------

@app.get("/api/staged")
def api_staged():
    """Every pending staged file plus its diff, in one round trip — the
    panel opens on a count, and a count you have to click to find out
    whether it means anything is barely better than the silent folder this
    replaces. Strictly read-only: nothing here writes, backs up or renames."""
    files = []
    for entry in staging.list_staged():
        entry = dict(entry)
        if not entry["error"]:
            try:
                entry["preview"] = staging.preview(entry["path"])
            except staging.StagedPathError as exc:
                entry["error"] = str(exc)
        files.append(entry)
    # An empty drop ([] — a sweep that legitimately found nothing) is listed
    # but doesn't count: the badge exists to say "there is something here to
    # decide on", and nagging about a file with no rows would teach you to
    # ignore it.
    pending = sum(1 for f in files if f["error"] or f["rows"])
    return jsonify({"files": files, "pending": pending})


@app.post("/api/staged/apply")
def api_staged_apply():
    """Apply one staged file. `selection` is a list of source-file row
    indices (omit it for all of them). The path is resolved inside the
    staging directory before anything opens it, so a crafted request can't
    reach an arbitrary file."""
    body = request.get_json(silent=True) or {}
    selection = body.get("selection")
    if selection is not None:
        if not isinstance(selection, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in selection
        ):
            return jsonify({"ok": False, "error": "bad_selection",
                            "message": "selection must be a list of row indices."}), 400
    try:
        result = staging.ingest(body.get("path") or "", selection)
    except staging.StagedPathError as exc:
        print(f"[staged] rejected path {body.get('path')!r}: {exc}")
        return jsonify({"ok": False, "error": "bad_path", "message": str(exc)}), 400
    if not result.get("ok"):
        return jsonify({"ok": False, "error": "unreadable",
                        "message": result.get("error")}), 400
    print(f"[staged] applied {result['filename']}: "
          f"{result['created']} created, {result['updated']} updated, "
          f"{result['skipped']} left unchecked")
    return jsonify(result)


# ------------------------------------------------------------------
# Server
# ------------------------------------------------------------------

def main():
    dbmod.init_db()
    url = f"http://{HOST}:{PORT}"
    print(f"Application cockpit -> {url}   (Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
