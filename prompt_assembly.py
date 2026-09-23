#!/usr/bin/env python3
"""
Prompt assembly for generation — the resident-context core (Phase 1).
=====================================================================
Every generation is parameterised: (role + category->master CV + writing-rules
+ JD text + about-me), assembled here so the context is never retyped.

Non-negotiable: writing-rules.md AND about-me.md are loaded VERBATIM into the
system prompt on every call. They are what keep drafts in the candidate's own
voice and off the AI-tell list. The master CV is the only source of
experience — the model is told to leave a visible [GAP] marker rather than
invent or inflate anything.

This module does NO network I/O. assemble() returns the messages; the API call
lives in generation.py (wired after the key is in .env). That keeps the prompt
build unit-testable on its own.
"""

import json
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


class AssemblyError(Exception):
    """Raised when a role can't be turned into a prompt (bad category, no JD)."""


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read(path):
    p = Path(path)
    if not p.exists():
        raise AssemblyError(f"required file missing: {path}")
    return p.read_text(encoding="utf-8")


def resolve_master_cv(category, config):
    """Return (path, text) for the category's master CV, or raise if the
    category has no mapping (Review) — the UI must ask for a category first."""
    cv_map = config.get("category_cv_map", {})
    if category not in cv_map or not cv_map[category]:
        raise AssemblyError(
            f"category '{category}' has no master CV — pick a real category "
            "(Leadership / Advisory / Senior IC) before generating."
        )
    path = Path(config["paths"]["cv_dir"]) / cv_map[category]
    return str(path), _read(path)


# ------------------------------------------------------------------
# System prompt: verbatim voice context + task-agnostic role framing
# ------------------------------------------------------------------

def build_role_frame(name, tagline_quote=None, preferred_name=None, category=None,
                      framing_map=None):
    """The task-agnostic role framing, parameterised by config['profile']:
    name is required (every profile has one); preferred_name defaults to
    name's first token but lets a nickname override it, for anyone who
    doesn't go by their own legal first name day to day.

    framing_map (config['profile']['framing']) is category-keyed positioning
    guidance — e.g. Leadership gets scale/org framing, Senior IC gets
    hands-on-craft framing — because how you lead a document should vary
    with the kind of role, the same way category_cv_map already varies
    which master CV gets used. When framing_map has an entry for `category`,
    it wins. tagline_quote is the fallback: a flat, category-blind
    positioning line, used when there's no framing_map entry for this
    category (including profiles that haven't set one up at all) or no
    category was given. Both are optional — a profile with neither gets no
    lead line at all, not an error."""
    short = preferred_name or name.split()[0]
    framing_map = framing_map or {}
    category_framing = framing_map.get(category) if category else None
    if category_framing:
        lead_line = f"- Framing for this role: {category_framing}\n"
    elif tagline_quote:
        lead_line = (
            f'- Lead with UX-as-strategy. {short}\'s line: "{tagline_quote}"\n'
            "  Never position them as a decorator.\n"
        )
    else:
        lead_line = ""
    return f"""\
You are drafting a job application document for {name}.

Absolute rules:
- Write in {short}'s voice. The documents above this line (their writing
  rules, their about-me, and their master CV) are binding. Follow the writing
  rules exactly — no em-dashes, no AI tells, no puffery, sentence-case headings,
  have a point of view.
{lead_line}- NEVER invent or inflate experience. Draw only from the master CV and stated
  facts. If the job asks for something the CV does not evidence, do not fabricate
  it — leave a literal marker `[GAP: what's missing]` so they can decide.
- Every specific detail — counts, scope claims ("three roles", "a team of five"),
  named systems or products, and characterizations of seniority or ownership
  ("established", "founded", "owned end-to-end") — must be traceable to an exact
  statement in the master CV. Summarizing across several bullets into one line is
  fine. Characterizing or elevating beyond what is written is not — that is
  invention even when it sounds plausible.
- Output only the finished document. No preamble, no "Here's the draft", no
  reasoning, no meta-commentary.
"""


def build_system_prompt(config, master_cv, category=None):
    """The stable, cacheable context: writing-rules.md verbatim + about-me.md
    verbatim + the category's master CV + the role frame. Everything here is
    identical across both doc types for a given category (the role frame now
    varies by category, same as the master CV already does), so it still
    caches and both the CV and cover-letter calls for the same role reuse it."""
    writing_rules = _read(config["paths"]["writing_rules"])
    about_me = _read(config["paths"]["about_me"])
    logo_map = config.get("company_logo_map", {}) or {}
    logo_keys = "\n".join(f"- {k}" for k in logo_map) or "(none configured)"
    profile = config.get("profile", {})
    name = profile.get("name", "the candidate")
    short = profile.get("preferred_name") or name.split()[0]
    role_frame = build_role_frame(name, profile.get("tagline_quote"), short,
                                   category=category, framing_map=profile.get("framing"))
    return (
        "# Writing rules (binding — follow exactly)\n\n"
        f"{writing_rules}\n\n"
        f"# About {short} (who you are drafting for)\n\n"
        f"{about_me}\n\n"
        "# Master CV (the ONLY source of experience — do not go beyond it)\n\n"
        f"{master_cv}\n\n"
        "# Company logo keys (CV generation only — logo_key must be one of these exact "
        "strings, or null if the experience company isn't listed here)\n\n"
        f"{logo_keys}\n\n"
        "# Your task framing\n\n"
        f"{role_frame}"
    )


# ------------------------------------------------------------------
# User prompt: master CV + JD + role metadata + the specific task
# ------------------------------------------------------------------

CV_TASK = """\
TASK: Produce a tailored CV for this role as a single emit_cv tool call. Fields:

- profile: 2-4 sentence positioning paragraph, tailored to this role.
- what_i_lead: capability lines relevant to this role, each {label, detail} —
  label a short 2-4 word heading (e.g. "Design organisations"), detail the
  supporting description. Matches the master CV's own bold-label bullet style.
- experience: one entry per role (reorder and reweight to lead with what's most
  relevant to this JD), each with role, company, logo_key, dates, context, and
  bullets (achievement bullets, reordered/reweighted to fit this JD).
- projects: OPTIONAL. Only for standalone projects (e.g. an open-source tool,
  an AI/technical side build) that don't fit the employment-history shape of
  "experience" — name, context, bullets. Omit the field, or use an empty
  array, if the master CV has nothing that belongs here. Never invent a
  project; populate only from what the master CV states.
- education: qualification, institution, dates.
- languages: language, level.

logo_key: the exact company_logo_map key listed in the system prompt if the
experience company matches one, else null. Never invent a key.

dates (experience only): the date range exactly as the master CV states it. If
the master CV gives no explicit date for an entry, use null — never infer or
approximate a boundary (do not write "Pre-2015" or anything similar). A null
date renders as no date line at all; that is the correct, honest result.

Rules: reorder and reweight to fit the JD, but every fact must trace to the
master CV above. Do not add companies, titles, dates, or achievements that are
not in it. Put "[GAP: ...]" inside the relevant field where the JD wants
something the CV doesn't evidence. Call emit_cv exactly once with the complete
document — no text outside the tool call.

Every field must be the native JSON type the tool schema declares — what_i_lead,
experience, education, and languages are real JSON arrays, each experience/
education/language entry a real JSON object. Never wrap array items in
XML-style tags (e.g. "<item>...</item>") and never emit an array as a single
string. That XML pattern belongs to an unrelated context and has nothing to
do with this tool call — ignore it as a formatting cue here.
"""

COVER_LETTER_TASK = """\
TASK: Write a one-page cover letter as markdown prose (not JSON).
- Direct first person, __PREFERRED_NAME__'s voice, no AI tells, no em-dashes.
- Open with why THIS company and THIS role — not "I am writing to apply".
- Lead with the framing set in your role frame above, then evidence from the CV.
- One page. Cut anything generic. Draw only from the master CV and stated facts;
  use [GAP: ...] rather than inventing anything.
- If a tailored CV for this role appears above, the reader already has it in
  front of them — do not re-narrate it. A paragraph whose entire content is a
  CV bullet in prose is a wasted paragraph. Naming one anchor fact briefly is
  fine where the argument genuinely needs it; what's not fine is restating
  the CV's own claims as if the reader hasn't seen them yet.
- Output only the letter body (no header block, no signature scaffolding unless
  it belongs in the prose).
"""

TASKS = {"cv": CV_TASK, "cover_letter": COVER_LETTER_TASK}


def _role_metadata_and_jd(role):
    """Shared by build_user_prompt and the plan-then-write builders below —
    the role-metadata block and JD-presence guardrail must stay identical
    across all three call shapes, not drift into three slightly different
    copies."""
    jd = (role.get("jd_text") or "").strip()
    if not jd:
        raise AssemblyError("no JD text on this role — fetch or paste the job "
                            "description before generating.")
    meta = (f"Company: {role.get('company','')}\n"
            f"Title: {role.get('title','')}\n"
            f"Category: {role.get('category','')}\n"
            f"Location: {role.get('location','') or '(not stated)'}")
    return meta, jd


def _tailored_cv_block(tailored_cv_text):
    """The CV-threading block (implementation pass 2, Task 2): the tailored
    CV already generated for this role, included so the cover-letter call —
    freeform or plan-then-write — can see exactly what the reader will see
    alongside the letter, and write around it instead of independently
    re-deriving the same facts from the master CV. Empty string, not None,
    when there's nothing to include, so callers can always concatenate it
    without a conditional."""
    if not tailored_cv_text:
        return ""
    return (
        "# Tailored CV already produced for this role\n\n"
        f"{tailored_cv_text}\n\n"
    )


def build_user_prompt(role, doc_type, config=None, tailored_cv_text=None):
    """The volatile per-role, per-task content: JD + role metadata + the task.
    Kept out of the cached system block so caching stays valid across roles.
    tailored_cv_text is cover-letter-only (ignored for doc_type == "cv",
    which has no CV yet to be aware of) — see _tailored_cv_block."""
    if doc_type not in TASKS:
        raise AssemblyError(f"unknown doc_type '{doc_type}'")
    meta, jd = _role_metadata_and_jd(role)
    config = config or load_config()
    profile = config.get("profile", {})
    short = profile.get("preferred_name") or profile.get("name", "the candidate").split()[0]
    task = TASKS[doc_type].replace("__PREFERRED_NAME__", short)
    cv_block = _tailored_cv_block(tailored_cv_text) if doc_type == "cover_letter" else ""
    return (
        "# Role metadata\n\n"
        f"{meta}\n\n"
        f"{cv_block}"
        "# Job description\n\n"
        f"{jd}\n\n"
        "# What to produce\n\n"
        f"{task}"
    )


# ------------------------------------------------------------------
# Plan-then-write cover letter path (implementation pass 2, Task 3).
# Two more user-prompt builders, same resident system prompt
# (build_system_prompt — unchanged, so this still caches across the plan
# call, the write call, and the CV call for the same role/category).
# ------------------------------------------------------------------

PLAN_TASK = """\
TASK: Plan a cover letter for this role as a single emit_cover_letter_plan
tool call. Do not write the letter itself yet, this is the plan it will be
written from.

Do these in order. Selection has to be checkable, not just plausible, so
each proof point has to trace to something real on both ends: a genuine JD
requirement and a genuine CV line.

- jd_requirements FIRST, before picking anything else: the JD's own top
  requirements, in its own words, copied close to verbatim, not your
  summary of them. This list is what proof-point selection has to answer
  to.
- jd_company_facts: anything specific to this company that the JD itself
  states (named products, launches, links, blog posts, stated ways of
  working), not a generic requirement (that belongs in jd_requirements) and
  not restated from company_evidence. An empty list is correct and honest
  when the JD carries no such specifics.
- opening_angle: what the first line argues. It must NOT be a
  characterisation of the company drawn from the job description (e.g.
  "Acme's bet is that..." or "Acme believes..."). That is paraphrase, not an
  angle. Ground it in a real jd_company_facts or company_evidence item if
  you have one, or in __PREFERRED_NAME__'s specific fit for this exact role
  otherwise.
- company_evidence: real, independently-known facts about the company from
  OUTSIDE the job description, something it shipped, published, or did that
  isn't already stated in the posting (that belongs in jd_company_facts
  instead). An empty list is correct and honest when you don't have a real
  outside fact available. Do not fill this field with JD paraphrase to
  avoid leaving it empty.
- proof_points: {min_points} to {max_points}. Each names a claim, the
  jd_requirement it answers (copied close to verbatim from your own
  jd_requirements list above), and the tailored-CV line it traces to
  (copied close to verbatim, not paraphrased, the write step paraphrases it
  later; this field has to be checkable against the actual CV first). Pick
  points that together cover what the JD actually asks for. Do not default
  to whichever two facts sound most impressive in isolation if the JD is
  asking for something else, for example a role that explicitly asks about
  managing people needs a proof point that answers that, not a second design-
  systems point instead of it.
- differentiation: one line, what makes __PREFERRED_NAME__'s angle
  different from a generic applicant's for this same role.
- target_words: the letter's target word count. Use {default_target} for
  this category unless the role's own posting gives a strong, specific
  reason for a different number (e.g. it also asks for several short essays
  and a shorter letter genuinely fits better).

Call emit_cover_letter_plan exactly once. No text outside the tool call.
"""

WRITE_TASK = """\
TASK: Write the cover letter as markdown prose (not JSON), from the plan
above.
- Direct first person, __PREFERRED_NAME__'s voice, no AI tells, no em-dashes.
- Write ONE continuous argument. Do not render the plan's fields in order or
  announce them ("First, ..." / "In terms of differentiation, ..."). The
  plan is scaffolding for you to write from, not a structure the reader
  should be able to see.
- Target {target_words} words. This is a real target, not a suggestion —
  cut anything generic to hit it.
- The reader already has the tailored CV above — do not re-narrate it. A
  paragraph whose entire content is a CV bullet in prose is a wasted
  paragraph. Naming one anchor fact briefly, where the argument genuinely
  needs it, is fine.
- Draw only from the plan's proof points, the tailored CV, and the master
  CV. Use [GAP: ...] rather than inventing anything.
- Output only the letter body (no header block, no signature scaffolding
  unless it belongs in the prose).
"""


def _short_name(config):
    profile = config.get("profile", {})
    return profile.get("preferred_name") or profile.get("name", "the candidate").split()[0]


def build_cover_letter_plan_user_prompt(role, config, tailored_cv_text, default_target_words,
                                         min_points=2, max_points=3):
    """User prompt for the plan step: role metadata + JD + the tailored CV
    (the model needs to see it to quote a real cv_source_line from it) +
    PLAN_TASK. No prior-attempt content here, a validation-failure retry
    appends its own note (generation.py), same shape as _generate_cv's.
    min_points/max_points (pass 4) must match whatever range the caller
    built the plan tool schema with, same range, same prompt text, or the
    model is told one thing and validated against another."""
    meta, jd = _role_metadata_and_jd(role)
    short = _short_name(config)
    cv_block = _tailored_cv_block(tailored_cv_text)
    task = (PLAN_TASK.replace("__PREFERRED_NAME__", short)
            .replace("{default_target}", str(default_target_words))
            .replace("{min_points}", str(min_points))
            .replace("{max_points}", str(max_points)))
    return (
        "# Role metadata\n\n"
        f"{meta}\n\n"
        f"{cv_block}"
        "# Job description\n\n"
        f"{jd}\n\n"
        "# What to produce\n\n"
        f"{task}"
    )


def build_cover_letter_write_user_prompt(role, config, tailored_cv_text, plan):
    """User prompt for the write step: the same role/JD/CV context plus the
    validated plan (as JSON, so the write step sees exactly what the plan
    step committed to) and WRITE_TASK. plan must already have passed
    cover_letter_schema.validate_plan_json and validate_source_lines — this
    function doesn't re-check it."""
    meta, jd = _role_metadata_and_jd(role)
    short = _short_name(config)
    cv_block = _tailored_cv_block(tailored_cv_text)
    task = WRITE_TASK.replace("__PREFERRED_NAME__", short).replace(
        "{target_words}", str(plan["target_words"]))
    return (
        "# Role metadata\n\n"
        f"{meta}\n\n"
        f"{cv_block}"
        "# Job description\n\n"
        f"{jd}\n\n"
        "# The plan to write from\n\n"
        f"{json.dumps(plan, indent=2)}\n\n"
        "# What to produce\n\n"
        f"{task}"
    )


def assemble(role, doc_type, config=None):
    """Return everything the API call needs: system, user, model, cv path/text.
    Raises AssemblyError on the guardrail cases (Review category, missing JD)."""
    config = config or load_config()
    category = role.get("category")
    cv_path, master_cv = resolve_master_cv(category, config)
    return {
        "model": config["model"],
        "system": build_system_prompt(config, master_cv, category=category),
        "user": build_user_prompt(role, doc_type, config),
        "master_cv_path": cv_path,
        "master_cv_text": master_cv,
        "doc_type": doc_type,
    }


if __name__ == "__main__":
    # Self-test: assemble a sample without calling any API. Proves the resident
    # context loads verbatim and the guardrails fire.
    cfg = load_config()
    sample = {
        "company": "Acme Bank", "title": "Head of Design",
        "category": "Leadership", "location": "Geneva",
        "jd_text": "We are looking for a Head of Design to lead our private-bank "
                   "digital experience across wealth platforms...",
    }
    a = assemble(sample, "cv", cfg)
    wr = _read(cfg["paths"]["writing_rules"])
    am = _read(cfg["paths"]["about_me"])
    print(f"model: {a['model']}")
    print(f"master CV: {a['master_cv_path']}")
    print(f"system chars: {len(a['system'])} | user chars: {len(a['user'])}")
    print(f"writing-rules verbatim in system: {wr.strip() in a['system']}")
    print(f"about-me verbatim in system:      {am.strip() in a['system']}")
    print(f"[GAP] instruction present:        {'[GAP' in a['system']}")
    print(f"no-em-dash instruction present:   {'em-dash' in a['system'].lower()}")
    print()
    for cat in ("Leadership", "Advisory", "Senior IC", "Review"):
        try:
            resolve_master_cv(cat, cfg)
            print(f"  category {cat:11} -> CV resolves")
        except AssemblyError as e:
            print(f"  category {cat:11} -> guardrail: {str(e)[:50]}...")
    try:
        build_user_prompt({"jd_text": ""}, "cv")
    except AssemblyError as e:
        print(f"  missing-JD guardrail: {str(e)[:50]}...")
