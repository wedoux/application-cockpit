#!/usr/bin/env python3
"""
Cover letter experiment 2 (implementation pass 4).
=======================================================
Same design as experiment 1 (pass 3), extended for the schema changes pass 4
made after experiment 1's own verdict found real evidentiary loss under
plan_then_write: jd_requirements/jd_company_facts/jd_requirement pointers,
a configurable proof-point range, and the cross-letter phrase check.

Before/after comparison: for every role that already has a stored cover
letter, regenerate the cover letter under plan_then_write mode, using that
role's EXISTING stored tailored CV as tailored_cv_text (not a freshly
regenerated one), so this measures the cover-letter-specific fixes in
isolation from CV-generation variance, and costs one call sequence per role
instead of two.

Read-mostly: makes real Anthropic API calls (real cost) but writes nothing
to cockpit.db. New drafts are scored and reported to
a report file outside the repo (plus a sibling full-text file); they are
never stored as new document versions, so a live application pipeline
running in parallel is untouched. If plan_then_write is later adopted for
real, that happens through /api/roles/<id>/generate with config.yaml's
cover_letter.mode flipped; this script measures, it doesn't adopt.

max_spend: checked before starting each role's generation, not mid-call: a
hard stop that reports partial results rather than either finishing an
over-budget run silently or aborting mid-call and losing the cost/usage
data from a call already in flight.

role_ids (pass 4, Task 5): run a named subset first, cheaply, before
spending on the rest. The three saved-full-text roles (findings the human
read is doing the judging on, not a metric) are hardcoded by role_id, not
doc_id, because a doc_id from a *new* generation doesn't exist yet when this
runs, and nothing in this script writes new documents rows, so role_id is
the only stable handle across before and after.
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

import db as dbmod
import generation
import prompt_assembly as pa
import style_gate as sg

# Reports land OUTSIDE the repository by default. They quote real postings
# and real generated letters, naming real companies, which is exactly what
# check_privacy.py exists to keep out of a public repo. Override with
# COVER_LETTER_REPORT_DIR if you want them somewhere else.
REPORT_DIR = Path(os.environ.get("COVER_LETTER_REPORT_DIR", Path(__file__).parent.parent))
REPORT_PATH = REPORT_DIR / "cover-letter-experiment-2.md"
FULLTEXT_PATH = REPORT_DIR / "cover-letter-experiment-2-fulltext.md"

# Three roles chosen to span the interesting cases, by role_id: one Senior
# IC role read closely during the investigation, one whose duplication was
# already low (tests whether the fix breaks something that wasn't broken),
# and one Leadership role with high duplication (tests the other end).
# Set these to role_ids from your own database.
SAVED_FULL_TEXT_ROLE_IDS = {89, 3, 83}


def _role_dict(conn, role_id):
    row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    return dict(row)


def _existing_cover_letters(conn, role_ids=None):
    if role_ids:
        placeholders = ",".join("?" * len(role_ids))
        return conn.execute(
            "SELECT d.id, d.role_id, r.company, r.title, r.category, d.content_md "
            "FROM documents d JOIN roles r ON r.id = d.role_id "
            f"WHERE d.doc_type = 'cover_letter' AND d.role_id IN ({placeholders}) ORDER BY d.id",
            list(role_ids),
        ).fetchall()
    return conn.execute(
        "SELECT d.id, d.role_id, r.company, r.title, r.category, d.content_md "
        "FROM documents d JOIN roles r ON r.id = d.role_id "
        "WHERE d.doc_type = 'cover_letter' ORDER BY d.id"
    ).fetchall()


def _latest_cv_text(conn, role_id):
    row = conn.execute(
        "SELECT content_md FROM documents WHERE role_id = ? AND doc_type = 'cv' "
        "ORDER BY version DESC LIMIT 1",
        (role_id,),
    ).fetchone()
    return row["content_md"] if row else None


def run_experiment(conn=None, config=None, max_spend=None, role_ids=None):
    """Returns {"results": [...], "total_cost": float, "stopped_early": bool,
    "stop_reason": str|None}. Each result dict always has role_id, meta,
    before (style_gate_row), before_paraphrase, before_openings,
    before_sentence_stats, before_text. On success it also has after,
    after_paraphrase, after_openings, after_sentence_stats, after_text,
    plan (the full validated plan dict, not just a summary — pass 3's own
    report found it couldn't answer a question about company_evidence
    because it hadn't kept this), cost_usd. On failure it has error
    instead. role_ids: only these roles, for the cheap targeted run
    (Task 5) before spending on the rest."""
    conn = conn or dbmod.connect()
    config = dict(config or pa.load_config())
    config["cover_letter"] = {**(config.get("cover_letter") or {}), "mode": "plan_then_write"}

    letters = _existing_cover_letters(conn, role_ids)
    results = []
    total_cost = 0.0
    stopped_early = False
    stop_reason = None

    for letter in letters:
        if max_spend is not None and total_cost > max_spend:
            stopped_early = True
            stop_reason = (f"cumulative spend ${total_cost:.4f} exceeded max_spend "
                            f"${max_spend:.4f} before role {letter['role_id']} "
                            f"({letter['company']})")
            break

        cv_text = _latest_cv_text(conn, letter["role_id"])
        meta = {"company": letter["company"], "title": letter["title"], "category": letter["category"]}
        before_text = letter["content_md"]
        entry = {
            "role_id": letter["role_id"], "meta": meta,
            "before": sg.style_gate_row(letter["id"], meta, before_text, cv_text),
            "before_paraphrase": sg.scan_paraphrase_duplication(before_text, cv_text),
            "before_openings": sg.paragraph_openings(before_text),
            "before_sentence_stats": sg.sentence_length_stats(before_text),
            "before_text": before_text,
        }

        role = _role_dict(conn, letter["role_id"])
        try:
            gen = generation.generate(role, "cover_letter", config, tailored_cv_text=cv_text)
        except Exception as e:  # noqa: BLE001, one role's failure must not lose the other 15
            entry["error"] = str(e)
            results.append(entry)
            continue

        total_cost += gen["cost_usd"]
        after_text = gen["content_md"]
        plan = gen.get("content_json") or {}
        after_row = sg.style_gate_row(letter["id"], meta, after_text, cv_text)
        after_row["company_evidence_empty"] = len(plan.get("company_evidence") or []) == 0
        after_row["jd_company_facts_empty"] = len(plan.get("jd_company_facts") or []) == 0
        after_row["plan_retried"] = gen.get("plan_retried", False)
        after_row["plan_retry_reason"] = gen.get("plan_retry_reason")
        after_row["plan_retry_reasons"] = gen.get("plan_retry_reasons", [])
        after_row["word_count_retried"] = gen.get("word_count_retried", False)

        entry.update({
            "after": after_row,
            "after_paraphrase": sg.scan_paraphrase_duplication(after_text, cv_text),
            "after_openings": sg.paragraph_openings(after_text),
            "after_sentence_stats": sg.sentence_length_stats(after_text),
            "after_text": after_text,
            "plan": plan,
            "cost_usd": gen["cost_usd"],
        })
        results.append(entry)

    return {"results": results, "total_cost": total_cost,
            "stopped_early": stopped_early, "stop_reason": stop_reason}


def _cross_letter_repeats(role_openings):
    """role_openings: [(role_id, [opening, ...]), ...]. Returns
    {opening: [role_ids]} for any opening used by 2+ different roles,
    the stronger seam-risk signal than a single letter repeating its own
    opening (that's covered by counting duplicates within one role's own
    list, done separately at call sites)."""
    counter = defaultdict(set)
    for role_id, openings in role_openings:
        for o in set(openings):
            if o:
                counter[o].add(role_id)
    return {o: sorted(ids) for o, ids in counter.items() if len(ids) > 1}


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else 0.0


def build_report(run_result, criterion_section):
    results = run_result["results"]
    ok = [r for r in results if "after" in r]
    errors = [r for r in results if "error" in r]

    lines = [criterion_section.rstrip(), "", "## Run"]
    lines.append(f"\n{len(results)} roles attempted, {len(ok)} succeeded, {len(errors)} failed. "
                 f"Total spend: ${run_result['total_cost']:.4f}.")
    if run_result["stopped_early"]:
        lines.append(f"\n**Stopped early: {run_result['stop_reason']}**")
    lines.append("\nNo historical cost data exists to correct: `documents` has no usage/cost "
                 "columns at all (confirmed reading db.py). Every prior generation's cost was "
                 "only ever printed to console, never persisted. The PRICING fix in generation.py "
                 "is forward-looking only; there's nothing stored to have been wrong.")

    if errors:
        lines.append("\n## Errors\n")
        for r in errors:
            lines.append(f"- role {r['role_id']} ({r['meta']['company']}): {r['error']}")

    # --- Counts nothing had asked for yet ---
    n_source_line = sum(1 for r in ok if "source_line" in r["after"]["plan_retry_reasons"])
    n_jd_trace = sum(1 for r in ok if "jd_requirement_trace" in r["after"]["plan_retry_reasons"])
    n_jd_pointer = sum(1 for r in ok if "jd_requirement_pointer" in r["after"]["plan_retry_reasons"])
    n_schema = sum(1 for r in ok if "schema" in r["after"]["plan_retry_reasons"])
    n_length_retry = sum(1 for r in ok if r["after"]["word_count_retried"])
    n_empty_evidence = sum(1 for r in ok if r["after"]["company_evidence_empty"])
    n_empty_jd_facts = sum(1 for r in ok if r["after"]["jd_company_facts_empty"])
    lines.append("\n## Counts\n")
    lines.append(f"- Plan retries forced by a schema error: {n_schema}/{len(ok)}")
    lines.append(f"- Plan retries forced by an invalid `cv_source_line` pointer: {n_source_line}/{len(ok)}")
    lines.append(f"- Plan retries forced by an invented `jd_requirement`/`jd_company_fact` "
                 f"(doesn't trace to the real JD): {n_jd_trace}/{len(ok)}")
    lines.append(f"- Plan retries forced by a proof point's `jd_requirement` not matching "
                 f"anything the plan itself extracted: {n_jd_pointer}/{len(ok)}")
    lines.append(f"- Write step needed the length retry: {n_length_retry}/{len(ok)}")
    lines.append(f"- `company_evidence` came back empty: {n_empty_evidence}/{len(ok)}")
    lines.append(f"- `jd_company_facts` came back empty: {n_empty_jd_facts}/{len(ok)}")

    # --- Per-role deltas, sorted regressions-first (duplication delta desc: positive = worse) ---
    lines.append("\n## Per-role deltas (sorted worst duplication delta first)\n")
    delta_rows = []
    for r in ok:
        b, a = r["before"], r["after"]
        b_above = int(b["dup_paraphrase_above"].split("/")[0]) if b["dup_paraphrase_above"] else 0
        a_above = int(a["dup_paraphrase_above"].split("/")[0]) if a["dup_paraphrase_above"] else 0
        delta_rows.append({
            "role_id": r["role_id"], "company": r["meta"]["company"], "category": r["meta"]["category"],
            "words_before": b["words"], "words_after": a["words"],
            "dup_above_before": b_above, "dup_above_after": a_above, "dup_above_delta": a_above - b_above,
            "negp_before": b["neg_parallelism_strict"], "negp_after": a["neg_parallelism_strict"],
            "company_evidence_empty": a["company_evidence_empty"],
        })
    delta_rows.sort(key=lambda x: x["dup_above_delta"], reverse=True)
    lines.append("| role | company | category | words before->after | dup-paras-above before->after | delta | neg-parallel before->after |")
    lines.append("|---|---|---|---|---|---|---|")
    for d in delta_rows:
        lines.append(
            f"| {d['role_id']} | {d['company']} | {d['category']} | "
            f"{d['words_before']}->{d['words_after']} | "
            f"{d['dup_above_before']}->{d['dup_above_after']} | {d['dup_above_delta']:+d} | "
            f"{d['negp_before']}->{d['negp_after']} |"
        )

    # --- Full per-paragraph paraphrase distribution, before and after, all roles ---
    lines.append("\n## Full per-paragraph paraphrase-duplication scores (not just the count above threshold)\n")
    lines.append("| role | company | before scores | after scores |")
    lines.append("|---|---|---|---|")
    for r in ok:
        b_scores = [pp["max_similarity"] for pp in r["before_paraphrase"]["per_paragraph"]]
        a_scores = [pp["max_similarity"] for pp in r["after_paraphrase"]["per_paragraph"]]
        lines.append(f"| {r['role_id']} | {r['meta']['company']} | "
                      f"{', '.join(f'{s:.2f}' for s in b_scores)} | "
                      f"{', '.join(f'{s:.2f}' for s in a_scores)} |")

    # --- Seam-risk proxies ---
    lines.append("\n## Seam-risk proxies\n")
    before_stdevs = [r["before_sentence_stats"]["stdev"] for r in ok]
    after_stdevs = [r["after_sentence_stats"]["stdev"] for r in ok]
    lines.append(f"Mean sentence-length stdev: before {_mean(before_stdevs)}, after {_mean(after_stdevs)} "
                 f"(delta {_mean(after_stdevs) - _mean(before_stdevs):+.3f}).")

    before_repeats = _cross_letter_repeats([(r["role_id"], r["before_openings"]) for r in ok])
    after_repeats = _cross_letter_repeats([(r["role_id"], r["after_openings"]) for r in ok])
    lines.append(f"\nCross-letter opening repeats (an opening pattern used by 2+ different letters): "
                 f"before {len(before_repeats)}, after {len(after_repeats)}.")
    if before_repeats:
        lines.append("\nBefore, shared by roles:")
        for o, ids in sorted(before_repeats.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {o!r}: {ids}")
    if after_repeats:
        lines.append("\nAfter, shared by roles:")
        for o, ids in sorted(after_repeats.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {o!r}: {ids}")

    lines.append("\n## Sentence-length stdev per role\n")
    lines.append("| role | company | stdev before | stdev after | delta |")
    lines.append("|---|---|---|---|---|")
    for r in ok:
        bs, as_ = r["before_sentence_stats"]["stdev"], r["after_sentence_stats"]["stdev"]
        lines.append(f"| {r['role_id']} | {r['meta']['company']} | {bs} | {as_} | {as_ - bs:+.2f} |")

    # --- Full-letter cross-letter phrase repetition (Task 4's actual
    # instrument, distinct from the opening-only proxy above: any 3-to-6-
    # word phrase shared across letters, not just how a paragraph starts) ---
    lines.append("\n## Cross-letter phrase repetition (full-letter n-grams, not just openings)\n")
    before_phrases = sg.scan_cross_letter_phrases({r["role_id"]: r["before_text"] for r in ok})
    after_phrases = sg.scan_cross_letter_phrases({r["role_id"]: r["after_text"] for r in ok})
    lines.append(f"Distinct shared phrases found: before {len(before_phrases)}, after {len(after_phrases)}.")
    if before_phrases:
        lines.append("\nBefore:")
        for p in before_phrases:
            lines.append(f"- {p['phrase']!r} ({p['n']} words): roles {p['letter_ids']}")
    if after_phrases:
        lines.append("\nAfter:")
        for p in after_phrases:
            lines.append(f"- {p['phrase']!r} ({p['n']} words): roles {p['letter_ids']}")

    # --- Full plans (pass 3's own gap: it couldn't confirm whether a
    # citation came from a populated field or the write step reaching past
    # an empty one — this closes that hole) ---
    lines.append("\n## Plans (full, per role)\n")
    for r in ok:
        p = r["plan"]
        lines.append(f"\n### {r['meta']['company']} (role {r['role_id']})\n")
        lines.append(f"- jd_requirements: {p.get('jd_requirements')}")
        lines.append(f"- jd_company_facts: {p.get('jd_company_facts')}")
        lines.append(f"- company_evidence: {p.get('company_evidence')}")
        for i, pt in enumerate(p.get("proof_points", [])):
            lines.append(f"- proof_points[{i}]: claim={pt.get('claim')!r}, "
                         f"jd_requirement={pt.get('jd_requirement')!r}, "
                         f"cv_source_line={pt.get('cv_source_line')!r}")
        lines.append(f"- differentiation: {p.get('differentiation')!r}")
        lines.append(f"- target_words: {p.get('target_words')}")

    lines.append(f"\nFull text for the three saved roles is in "
                 f"[{FULLTEXT_PATH.name}]({FULLTEXT_PATH.name}).")

    return "\n".join(lines)


def build_fulltext(run_result):
    results = {r["role_id"]: r for r in run_result["results"] if "after" in r}
    lines = ["# Cover letter experiment 2: full text, before and after\n",
             "The three roles picked for a full read: one Senior IC role read closely "
             "during the investigation, one whose duplication was already low (tests "
             "whether the fix breaks something that wasn't broken), and one Leadership "
             "role with high duplication (tests the other end).\n"]
    for role_id in sorted(SAVED_FULL_TEXT_ROLE_IDS):
        r = results.get(role_id)
        if not r:
            lines.append(f"\n## Role {role_id}: not available (failed or not in this run)\n")
            continue
        lines.append(f"\n## {r['meta']['company']}: {r['meta']['title']} (role {role_id})\n")
        lines.append(f"### Before ({r['before']['words']} words)\n")
        lines.append(r["before_text"])
        lines.append(f"\n### After ({r['after']['words']} words)\n")
        lines.append(r["after_text"])
    return "\n".join(lines)


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        print("This makes real Anthropic API calls against every role with a stored cover "
              "letter and costs real money. Re-run with --yes to proceed.")
        sys.exit(1)

    MAX_SPEND = 4.00  # 2x the $2.00 upper end of the pre-run estimate, a hard stop.
    existing = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    criterion_marker = "\n---\n"
    criterion_section = existing.split(criterion_marker)[0] + criterion_marker if criterion_marker in existing else existing
    if not criterion_section.strip():
        print(f"No pre-registered criterion found in {REPORT_PATH}, write it first.")
        sys.exit(1)

    run_result = run_experiment(max_spend=MAX_SPEND)
    report = build_report(run_result, criterion_section)
    fulltext = build_fulltext(run_result)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    FULLTEXT_PATH.write_text(fulltext, encoding="utf-8")
    print(f"Wrote {REPORT_PATH} and {FULLTEXT_PATH}, total spend ${run_result['total_cost']:.4f}"
          + (f", STOPPED EARLY: {run_result['stop_reason']}" if run_result["stopped_early"] else ""))
