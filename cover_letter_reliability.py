#!/usr/bin/env python3
"""
Cover-letter reliability harness (pass 5).
=========================================
Pass 4 left one question open: plan-then-write selects the right proof
points and then does not reliably deliver them. One validated plan,
two runs, one letter naming the 18 designers managed and one never
mentioning management at all. A single good run cannot answer that, because
the failure IS the variance.

So this does not compare before against after. It runs ONE plan per role and
writes from it five times, which isolates write-step variance from plan
variance: every run in a role's group is conditioned on byte-identical
input, so any difference between them is the write step alone.

Shape per role: run 1 goes through the full path
(generation._generate_cover_letter_plan_then_write) and its plan is kept;
runs 2 to 5 call generation.write_letter_from_plan directly with that same
plan. Nothing here reimplements the write step or its retry loops. That
matters more than usual for this script: a harness with its own copy would
be measuring a write step that isn't the one that ships, and the drift would
look exactly like a result.

Measured every run, not just at the end, because pass 5's stopping rule
turns on whether coverage and texture move together or against each other:
  - commitment coverage on the FIRST attempt and after the retry, separately
  - sentence-length stdev (the texture proxy that regressed in pass 4)
  - cross-letter phrases shared with the already-stored letters

Spend: checked before every write call, never mid-call. Estimated $0.70 for
3 plans and 15 writes plus expected retries; MAX_SPEND is 2x that, and the
run stops and reports what it has rather than quietly continuing.

Reports land outside the repository by default (same convention as
cover_letter_experiment.py): real letters, real JDs, real company names.
Override with COVER_LETTER_REPORT_DIR.
"""

import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

import db as dbmod
import generation
import prompt_assembly as pa
import style_gate

REPORT_DIR = Path(os.environ.get("COVER_LETTER_REPORT_DIR", Path(__file__).parent.parent))
REPORT_PATH = REPORT_DIR / "cover-letter-reliability.md"
FULLTEXT_PATH = REPORT_DIR / "cover-letter-reliability-fulltext.md"

# Set these to role_ids from your own database.
ROLE_IDS = [3, 83, 89]
RUNS_PER_ROLE = 5

ESTIMATED_SPEND = 0.70
MAX_SPEND = 1.40  # 2x the estimate, the pre-agreed hard stop


def _role_dict(conn, role_id):
    r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    return dict(r) if r else None


def _latest_cv_text(conn, role_id):
    r = conn.execute(
        "SELECT content_md FROM documents WHERE role_id = ? AND doc_type = 'cv' "
        "ORDER BY version DESC LIMIT 1", (role_id,),
    ).fetchone()
    return r["content_md"] if r else None


def _stored_corpus(conn, exclude_role_id):
    """Every stored cover letter for a different role, shaped exactly as
    app.cross_letter_corpus shapes it: {document_id: text}, not a list.
    scan_new_letter_against_corpus needs the ids to report which stored
    letters a repeated phrase came from. Same query as the live path, kept
    here rather than imported because importing app.py builds a Flask app as
    a side effect; if that query ever changes, this one changes with it."""
    rows = conn.execute(
        "SELECT id, content_md FROM documents WHERE doc_type = 'cover_letter' "
        "AND role_id != ?", (exclude_role_id,),
    ).fetchall()
    return {r["id"]: r["content_md"] for r in rows if r["content_md"]}


def _coverage_summary(cov):
    """Flatten a coverage dict to the few numbers the report tabulates.
    checkable is carried through deliberately: covered=True with
    checkable=0 means nothing was checked, not that the plan was kept."""
    if not cov:
        return {"covered": None, "missing": None, "checkable": 0, "total": 0, "misses": []}
    return {
        "covered": cov["covered"],
        "missing": cov["points_missing_numbers"],
        "checkable": cov["checkable_points"],
        "total": cov["proof_points_total"],
        "misses": cov["misses"],
        "per_point": [
            {"index": p["index"], "numbers": p["numbers"],
             "found": p["numbers_found"], "ok": p["numbers_ok"]}
            for p in cov["per_proof_point"]
        ],
    }


def _measure(text, plan, corpus):
    sent = style_gate.sentence_length_stats(text)
    return {
        "words": len(text.split()),
        "stdev": sent["stdev"],
        "mean_sentence": sent["mean"],
        "sentences": sent["n"],
        "cross_letter_phrases": len(
            style_gate.scan_new_letter_against_corpus(text, corpus) if corpus else []
        ),
    }


def run(conn=None, config=None, role_ids=None, runs_per_role=RUNS_PER_ROLE,
        max_spend=MAX_SPEND):
    close_conn = conn is None
    conn = conn or dbmod.connect()
    config = config or pa.load_config()
    role_ids = role_ids or ROLE_IDS
    client = anthropic.Anthropic()
    model = config["model"]

    total_cost = 0.0
    stop_reason = None
    results = []

    for role_id in role_ids:
        if stop_reason:
            break
        role = _role_dict(conn, role_id)
        if not role:
            print(f"[reliability] role {role_id} not found, skipping")
            continue
        category = role.get("category")
        tailored_cv = _latest_cv_text(conn, role_id)
        _, master_cv_text = pa.resolve_master_cv(category, config)
        system = pa.build_system_prompt(config, master_cv_text, category=category)
        _, tolerance_pct = generation._cover_letter_targets(config, category)
        corpus = _stored_corpus(conn, role_id)

        role_runs = []
        plan = None
        write_user = None

        for i in range(runs_per_role):
            if total_cost > max_spend:
                stop_reason = (
                    f"cumulative spend ${total_cost:.4f} passed max_spend "
                    f"${max_spend:.2f} before run {i + 1} of role {role_id}"
                )
                print(f"[reliability] STOPPING: {stop_reason}")
                break

            if i == 0:
                # Full path once: this is what produces the plan every other
                # run in this group is conditioned on.
                out = generation._generate_cover_letter_plan_then_write(
                    client, role, config, system, tailored_cv)
                plan = out["content_json"]
                write_user = pa.build_cover_letter_write_user_prompt(
                    role, config, tailored_cv, plan)
                text = out["content_md"]
            else:
                out = generation.write_letter_from_plan(
                    client, model, system, write_user, plan, tolerance_pct)
                text = out["text"]

            total_cost = round(total_cost + out["cost_usd"], 4)
            m = _measure(text, plan, corpus)
            role_runs.append({
                "run": i + 1,
                "text": text,
                "cost_usd": out["cost_usd"],
                "first_pass": _coverage_summary(out["first_pass_coverage"]),
                "final": _coverage_summary(out["commitment_coverage"]),
                "coverage_retried": out["coverage_retried"],
                "word_count_retried": out["word_count_retried"],
                **m,
            })
            print(f"[reliability] role {role_id} run {i + 1}/{runs_per_role}: "
                  f"{m['words']}w stdev={m['stdev']} "
                  f"first_pass_covered={role_runs[-1]['first_pass']['covered']} "
                  f"final_covered={role_runs[-1]['final']['covered']} "
                  f"(${total_cost:.4f})")

        results.append({
            "role_id": role_id,
            "company": role.get("company"),
            "title": role.get("title"),
            "category": category,
            "plan": plan,
            "runs": role_runs,
            "baseline_stdevs": [
                style_gate.sentence_length_stats(r["content_md"])["stdev"]
                for r in conn.execute(
                    "SELECT content_md FROM documents WHERE role_id = ? "
                    "AND doc_type = 'cover_letter' ORDER BY version",
                    (role_id,)).fetchall()
            ],
        })

    if close_conn:
        conn.close()
    return {
        "results": results,
        "total_cost": total_cost,
        "stop_reason": stop_reason,
        "estimated_spend": ESTIMATED_SPEND,
        "max_spend": max_spend,
        "runs_per_role": runs_per_role,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_report(res):
    L = []
    a = L.append
    a("")
    a("## Run")
    a("")
    a(f"Generated {res['generated_at']}. Spend ${res['total_cost']:.4f} against an "
      f"estimate of ${res['estimated_spend']:.2f} and a hard stop at ${res['max_spend']:.2f}.")
    if res["stop_reason"]:
        a("")
        a(f"**Stopped early:** {res['stop_reason']}")
    a("")

    a("## Per-run detail")
    a("")
    a("`first` is the write step's own output. `final` is after the one "
      "commitment retry, where one ran. `checkable` is how many of the plan's "
      "proof points carry a figure this check can verify at all.")
    a("")
    a("| role | run | words | stdev | x-letter | first covered | missing | final covered | retried | checkable/total |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for r in res["results"]:
        for run_ in r["runs"]:
            fp, fin = run_["first_pass"], run_["final"]
            a(f"| {r['company']} ({r['role_id']}) | {run_['run']} | {run_['words']} | "
              f"{run_['stdev']} | {run_['cross_letter_phrases']} | {fp['covered']} | "
              f"{fp['missing']} | {fin['covered']} | {run_['coverage_retried']} | "
              f"{fin['checkable']}/{fin['total']} |")
    a("")

    a("## Coverage distribution, per role")
    a("")
    a("| role | first-pass covered | after retry | runs |")
    a("|---|---|---|---|")
    for r in res["results"]:
        n = len(r["runs"])
        if not n:
            continue
        fp = sum(1 for x in r["runs"] if x["first_pass"]["covered"])
        fin = sum(1 for x in r["runs"] if x["final"]["covered"])
        a(f"| {r['company']} ({r['role_id']}) | {fp}/{n} | {fin}/{n} | {n} |")
    a("")

    a("## Per-proof-point delivery")
    a("")
    a("Each row is one proof point across that role's runs: how many runs "
      "carried its committed figures into the prose on the first attempt.")
    a("")
    a("| role | point | figures | first-pass landed | after retry |")
    a("|---|---|---|---|---|")
    for r in res["results"]:
        if not r["runs"]:
            continue
        n = len(r["runs"])
        n_points = r["runs"][0]["final"]["total"]
        for idx in range(n_points):
            figs, fp_ok, fin_ok = [], 0, 0
            for run_ in r["runs"]:
                pp = next((p for p in run_["first_pass"]["per_point"] if p["index"] == idx), None)
                qq = next((p for p in run_["final"]["per_point"] if p["index"] == idx), None)
                if pp:
                    figs = pp["numbers"]
                    fp_ok += 1 if pp["ok"] else 0
                if qq and qq["ok"]:
                    fin_ok += 1
            label = ", ".join(figs) if figs else "none (not checkable)"
            a(f"| {r['company']} ({r['role_id']}) | {idx + 1} | {label} | {fp_ok}/{n} | {fin_ok}/{n} |")
    a("")

    a("## Texture, every run")
    a("")
    a("Baseline stdev is the mean across that role's already-stored letters, "
      "the same figures pass 4 compared against.")
    a("")
    a("| role | baseline stdev | run stdevs | mean after | delta |")
    a("|---|---|---|---|---|")
    for r in res["results"]:
        if not r["runs"] or not r["baseline_stdevs"]:
            continue
        base = round(statistics.fmean(r["baseline_stdevs"]), 2)
        stdevs = [x["stdev"] for x in r["runs"]]
        after = round(statistics.fmean(stdevs), 2)
        a(f"| {r['company']} ({r['role_id']}) | {base} | "
          f"{', '.join(str(s) for s in stdevs)} | {after} | {round(after - base, 2):+} |")
    a("")

    all_runs = [x for r in res["results"] for x in r["runs"]]
    if all_runs:
        overall = round(statistics.fmean([x["stdev"] for x in all_runs]), 2)
        a(f"Mean sentence-length stdev across all {len(all_runs)} runs: **{overall}**.")
        a("")
        a(f"Mean cross-letter phrase count: "
          f"{round(statistics.fmean([x['cross_letter_phrases'] for x in all_runs]), 2)}.")
        a("")
    return "\n".join(L)


def build_fulltext(res):
    L = ["# Cover letter reliability: full text and plans", ""]
    for r in res["results"]:
        L.append(f"## {r['company']}: {r['title']} (role {r['role_id']})")
        L.append("")
        L.append("### Plan (one plan, every run below written from it)")
        L.append("")
        L.append("```json")
        L.append(json.dumps(r["plan"], indent=2, ensure_ascii=False))
        L.append("```")
        L.append("")
        for run_ in r["runs"]:
            L.append(f"### Run {run_['run']} ({run_['words']} words, stdev {run_['stdev']}, "
                     f"retried={run_['coverage_retried']})")
            L.append("")
            L.append(run_["text"])
            L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    only = [int(x) for x in sys.argv[1:]] or None
    res = run(role_ids=only)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    existing = REPORT_PATH.read_text() if REPORT_PATH.exists() else ""
    REPORT_PATH.write_text(existing + build_report(res))
    FULLTEXT_PATH.write_text(build_fulltext(res))
    print(f"\n[reliability] report -> {REPORT_PATH}")
    print(f"[reliability] full text -> {FULLTEXT_PATH}")
    print(f"[reliability] total ${res['total_cost']:.4f}")
