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

def build_role_frame(name, tagline_quote=None, preferred_name=None):
    """The task-agnostic role framing, parameterised by config['profile']:
    name is required (every profile has one); preferred_name defaults to
    name's first token but lets a nickname override it, for anyone who
    doesn't go by their own legal first name day to day; tagline_quote is
    optional — a personal positioning line isn't something every profile
    has, so the rule it drives is only included when configured."""
    short = preferred_name or name.split()[0]
    lead_line = (
        f'- Lead with UX-as-strategy. {short}\'s line: "{tagline_quote}"\n'
        "  Never position them as a decorator.\n"
        if tagline_quote else ""
    )
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


def build_system_prompt(config, master_cv):
    """The stable, cacheable context: writing-rules.md verbatim + about-me.md
    verbatim + the category's master CV + the role frame. Everything here is
    identical across both doc types (and across roles in the same category), so
    it caches and both the CV and cover-letter calls reuse it."""
    writing_rules = _read(config["paths"]["writing_rules"])
    about_me = _read(config["paths"]["about_me"])
    logo_map = config.get("company_logo_map", {}) or {}
    logo_keys = "\n".join(f"- {k}" for k in logo_map) or "(none configured)"
    profile = config.get("profile", {})
    name = profile.get("name", "the candidate")
    short = profile.get("preferred_name") or name.split()[0]
    role_frame = build_role_frame(name, profile.get("tagline_quote"), short)
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
- Lead with the strategic angle (UX as strategy), then evidence from the CV.
- One page. Cut anything generic. Draw only from the master CV and stated facts;
  use [GAP: ...] rather than inventing anything.
- Output only the letter body (no header block, no signature scaffolding unless
  it belongs in the prose).
"""

TASKS = {"cv": CV_TASK, "cover_letter": COVER_LETTER_TASK}


def build_user_prompt(role, doc_type, config=None):
    """The volatile per-role, per-task content: JD + role metadata + the task.
    Kept out of the cached system block so caching stays valid across roles."""
    if doc_type not in TASKS:
        raise AssemblyError(f"unknown doc_type '{doc_type}'")
    jd = (role.get("jd_text") or "").strip()
    if not jd:
        raise AssemblyError("no JD text on this role — fetch or paste the job "
                            "description before generating.")
    meta = (f"Company: {role.get('company','')}\n"
            f"Title: {role.get('title','')}\n"
            f"Category: {role.get('category','')}\n"
            f"Location: {role.get('location','') or '(not stated)'}")
    config = config or load_config()
    profile = config.get("profile", {})
    short = profile.get("preferred_name") or profile.get("name", "the candidate").split()[0]
    task = TASKS[doc_type].replace("__PREFERRED_NAME__", short)
    return (
        "# Role metadata\n\n"
        f"{meta}\n\n"
        "# Job description\n\n"
        f"{jd}\n\n"
        "# What to produce\n\n"
        f"{task}"
    )


def assemble(role, doc_type, config=None):
    """Return everything the API call needs: system, user, model, cv path/text.
    Raises AssemblyError on the guardrail cases (Review category, missing JD)."""
    config = config or load_config()
    cv_path, master_cv = resolve_master_cv(role.get("category"), config)
    return {
        "model": config["model"],
        "system": build_system_prompt(config, master_cv),
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
