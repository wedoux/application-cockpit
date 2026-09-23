#!/usr/bin/env python3
"""
Cover letter plan schema (implementation passes 2 and 4).
=============================================================
The plan step of the plan-then-write cover letter path: a small, forced tool call, structured the same way
cv_schema.py's emit_cv is: validated on receipt, retried once with the
validation error fed back if it fails (generation.py's
_generate_cover_letter_plan_then_write mirrors _generate_cv's retry shape).

Why a plan step and not a single emit_cover_letter call that renders
straight to prose: a letter is read as one continuous argument, and slots
rendered independently tend to produce identical paragraph-opening rhythms,
the "assembled" texture writing-rules.md's own thesis warns against. The
plan is structured because its fields need to be checked (does a
cv_source_line actually trace to something in the CV, does a proof point
actually answer something the JD asked for, is company_evidence genuinely
empty rather than JD-paraphrase dressed as evidence); the letter itself,
written from the validated plan in a second free-prose call, isn't.

Pass 4 added jd_requirements, jd_company_facts, and a jd_requirement pointer
on each proof point, after an ablation found that neither a wider word budget nor more proof-
point slots changed what the model selected. The same two default story
beats (design systems, AI/trust) came back regardless of headroom, even on
a Leadership role whose JD explicitly and emphatically asked for formal
people-management experience, and got neither a proof point about it nor a
mention in the prose. Selection wasn't constrained, it was unguided: nothing in
the plan step ever asked the model to check its picks against what the JD
actually asked for. jd_requirements makes that check possible; each proof
point's jd_requirement pointer makes it checkable per point, the same way
cv_source_line already makes traceability to the CV checkable per point.

Two "trace" validators now, not one, both built on the same substring-
containment method (validate_source_lines from pass 2, validate_jd_fields
new in pass 4). A claim is only as good as its two ends: does it come from
somewhere real (the CV or the JD), and does it answer something real (a
stated JD requirement). validate_proof_point_requirements checks the third
leg: that each proof point's jd_requirement pointer actually names one of
the plan's own extracted jd_requirements, not an invented one.
"""

import re

from style_gate import extract_cv_lines

PLAN_TOOL_NAME = "emit_cover_letter_plan"

DEFAULT_MIN_PROOF_POINTS = 2
DEFAULT_MAX_PROOF_POINTS = 3


def build_plan_tool(min_points=DEFAULT_MIN_PROOF_POINTS, max_points=DEFAULT_MAX_PROOF_POINTS):
    """The tool schema is built per call, not a fixed module constant,
    because the proof-point range is category-aware (config.yaml's
    cover_letter.proof_points): a Leadership letter juggling scale,
    people-management, and AI work may genuinely need a third point where a
    Senior IC letter doesn't. The ablation (see module docstring) found the
    model doesn't reach for extra slots on its own even when they're
    available, so widening the range is not expected to change length by
    itself; it exists so a role that genuinely needs 3 distinct, well-
    targeted points isn't structurally prevented from having them."""
    return {
        "name": PLAN_TOOL_NAME,
        "description": ("Emit the cover letter's plan as structured content before writing it. "
                         "Call this exactly once."),
        "input_schema": {
            "type": "object",
            "properties": {
                "opening_angle": {
                    "type": "string",
                    "description": ("What the letter's first line argues. This must NOT be a "
                                     "characterisation of the company drawn from the job description "
                                     "(e.g. 'Acme's bet is that...'), that is paraphrase, not an angle. "
                                     "Ground it in a real jd_company_facts or company_evidence item if "
                                     "one exists, or in the candidate's own specific fit otherwise."),
                },
                "jd_requirements": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("The JD's own top requirements, in ITS words, copied close to "
                                     "verbatim, not your paraphrase of them. Extract these BEFORE "
                                     "choosing proof points; this list is what proof-point selection "
                                     "has to answer to, not a summary written after the fact."),
                },
                "jd_company_facts": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Anything specific to this company that the JD itself states: "
                                     "named products, launches, links, blog posts, stated ways of "
                                     "working. NOT a generic requirement (that belongs in "
                                     "jd_requirements) and NOT restated from company_evidence. An "
                                     "empty list is the correct, honest answer when the JD carries no "
                                     "such specifics; don't invent one to avoid leaving it empty."),
                },
                "company_evidence": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Real, independently-known facts about the company from OUTSIDE "
                                     "the job description, something it shipped, published, or did "
                                     "that isn't stated in the posting itself (that belongs in "
                                     "jd_company_facts instead). An empty array is the correct, honest "
                                     "output when no such outside fact is available; do not fill this "
                                     "with JD paraphrase to avoid leaving it empty."),
                },
                "proof_points": {
                    "type": "array", "minItems": min_points, "maxItems": max_points,
                    "description": (f"Between {min_points} and {max_points}. Each names a claim, the "
                                     "jd_requirement it answers, and the cv_source_line it's evidenced "
                                     "by. Pick points that together cover the JD's own stated "
                                     "requirements. Do not default to whichever two facts sound most "
                                     "impressive if a different requirement is what this specific role "
                                     "actually asks for."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string", "description": "What this proof point argues."},
                            "jd_requirement": {
                                "type": "string",
                                "description": ("Which entry in jd_requirements this proof point "
                                                 "answers, copied close to verbatim from that list. "
                                                 "A proof point that doesn't trace to a real JD "
                                                 "requirement is a selection error, not a stylistic "
                                                 "choice."),
                            },
                            "cv_source_line": {
                                "type": "string",
                                "description": ("The actual tailored-CV bullet this traces to, copied "
                                                 "close to verbatim, not a paraphrase. The write step "
                                                 "paraphrases it into prose later; this field exists so "
                                                 "the pointer itself can be checked against the real CV "
                                                 "before that happens."),
                            },
                        },
                        "required": ["claim", "jd_requirement", "cv_source_line"],
                    },
                },
                "differentiation": {
                    "type": "string",
                    "description": "One line: what makes this candidate's angle different from a generic applicant's.",
                },
                "target_words": {
                    "type": "integer",
                    "description": ("The letter's target word count. Use the configured default for "
                                     "this category unless the role's own posting gives a strong, "
                                     "specific reason for a different number."),
                },
            },
            "required": ["opening_angle", "jd_requirements", "jd_company_facts", "company_evidence",
                         "proof_points", "differentiation", "target_words"],
        },
    }


# A default-range instance for callers that just need the shape (e.g. a
# quick import-time check) without caring about the configured range.
PLAN_TOOL = build_plan_tool()


def _err(errors, path, msg):
    errors.append(f"{path}: {msg}")


def _check_str(v, path, errors):
    if not isinstance(v, str) or not v.strip():
        _err(errors, path, "expected a non-empty string")


def _check_str_list(v, path, errors):
    if not isinstance(v, list):
        _err(errors, path, "expected a list of strings (an empty list is valid)")
    else:
        for i, item in enumerate(v):
            _check_str(item, f"{path}[{i}]", errors)


def validate_plan_json(data, min_points=DEFAULT_MIN_PROOF_POINTS, max_points=DEFAULT_MAX_PROOF_POINTS):
    """Return a list of error strings; empty list means valid. Mirrors
    cv_schema.validate_cv_json's shape and conventions exactly. min_points/
    max_points must match whatever range build_plan_tool() was called with
    for this same request, or a plan the model legitimately built within
    its allowed range would fail validation against a different range."""
    errors = []
    if not isinstance(data, dict):
        return ["root: expected a JSON object"]

    _check_str(data.get("opening_angle"), "opening_angle", errors)
    _check_str_list(data.get("jd_requirements"), "jd_requirements", errors)
    _check_str_list(data.get("jd_company_facts"), "jd_company_facts", errors)
    _check_str_list(data.get("company_evidence"), "company_evidence", errors)

    points = data.get("proof_points")
    if not isinstance(points, list) or not (min_points <= len(points) <= max_points):
        _err(errors, "proof_points",
             f"expected a list of {min_points} to {max_points} "
             "{claim, jd_requirement, cv_source_line} objects")
    else:
        for i, p in enumerate(points):
            path = f"proof_points[{i}]"
            if not isinstance(p, dict):
                _err(errors, path, "expected an object with claim, jd_requirement, and cv_source_line")
                continue
            _check_str(p.get("claim"), f"{path}.claim", errors)
            _check_str(p.get("jd_requirement"), f"{path}.jd_requirement", errors)
            _check_str(p.get("cv_source_line"), f"{path}.cv_source_line", errors)

    _check_str(data.get("differentiation"), "differentiation", errors)

    target_words = data.get("target_words")
    if not isinstance(target_words, int) or isinstance(target_words, bool) or not (50 <= target_words <= 1500):
        _err(errors, "target_words", "expected an integer between 50 and 1500")

    return errors


_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _normalize(text):
    return " ".join(_WORD_RE.findall((text or "").lower()))


def validate_source_lines(plan, cv_text):
    """For each proof point, confirm cv_source_line actually traces to a real
    line in the tailored CV: substring containment either direction (the
    model may quote a fragment of a longer bullet, or occasionally the whole
    bullet plus a little), normalized on word boundaries so punctuation and
    case differences don't cause a false failure. Returns a list of error
    strings, empty if every pointer checks out. Only meaningful once
    validate_plan_json(plan) has already passed. Assumes proof_points is
    the right shape."""
    errors = []
    cv_lines = [_normalize(l) for l in extract_cv_lines(cv_text)]
    if not cv_lines:
        return ["cv_source_line: tailored CV has no bullet lines to validate against"]
    for i, point in enumerate(plan.get("proof_points", [])):
        target = _normalize(point.get("cv_source_line", ""))
        if not target:
            continue  # already caught by validate_plan_json
        if not any(target in line or line in target for line in cv_lines):
            _err(errors, f"proof_points[{i}].cv_source_line",
                 f"does not match any line in the tailored CV: {point.get('cv_source_line', '')!r}")
    return errors


JD_FIELD_OVERLAP_THRESHOLD = 0.7


def _word_overlap_ratio(claim, source_text):
    """Fraction of claim's distinct words that also appear anywhere in
    source_text, order-independent. Not the same test as
    validate_source_lines' exact substring containment, on purpose: a CV
    bullet is a clean, short, already-discrete unit a model quotes reliably
    verbatim, but a JD is flowing prose, and asking a model to extract "the
    JD's own words, close to verbatim" from prose reliably produces light
    paraphrase even when the extraction is completely genuine ("Reports to"
    -> "Reports directly to", "our product designer" -> "the product
    designer", clause reordering). Calibrated against a real failure: an
    earlier substring-containment version of this check rejected all six of
    one real JD's company facts and one real requirement, every one
    confirmed by hand to be a genuine, well-grounded extraction, because
    none was a character-exact substring of the posting. Word overlap on
    that same real data scored 0.82-1.00; a fabricated claim sharing almost
    no vocabulary with the source scores near 0.0. 0.7 sits well clear of
    both."""
    claim_words = set(_WORD_RE.findall(claim.lower()))
    if not claim_words:
        return 1.0
    source_words = set(_WORD_RE.findall(source_text.lower()))
    return len(claim_words & source_words) / len(claim_words)


def validate_jd_fields(plan, jd_text, threshold=JD_FIELD_OVERLAP_THRESHOLD):
    """Confirm jd_requirements and jd_company_facts actually trace to the
    real JD text via word-overlap ratio (see _word_overlap_ratio), not
    exact substring containment. Catches a "requirement" or "company fact"
    the model invented rather than extracted, without punishing the
    ordinary light paraphrase real extraction from prose produces. Only
    meaningful once validate_plan_json has already passed."""
    errors = []
    if not (jd_text or "").strip():
        return ["jd_requirements: role has no JD text to validate against"]
    for i, req in enumerate(plan.get("jd_requirements", [])):
        if req and _word_overlap_ratio(req, jd_text) < threshold:
            _err(errors, f"jd_requirements[{i}]",
                 f"does not appear in the job description: {req!r}")
    for i, fact in enumerate(plan.get("jd_company_facts", [])):
        if fact and _word_overlap_ratio(fact, jd_text) < threshold:
            _err(errors, f"jd_company_facts[{i}]",
                 f"does not appear in the job description: {fact!r}")
    return errors


def validate_proof_point_requirements(plan):
    """Confirm each proof point's jd_requirement pointer actually names one
    of the plan's own jd_requirements entries: internal consistency, no
    external text needed. This is the direct fix for the ablation's
    finding: a proof point that doesn't trace to a real, extracted JD
    requirement is a selection failure the schema can now catch, the same
    way an unmatched cv_source_line already was. Only meaningful once
    validate_plan_json has already passed."""
    errors = []
    requirements = [_normalize(r) for r in plan.get("jd_requirements", [])]
    if not requirements:
        return ["proof_points: jd_requirements is empty, so no proof point can trace to one"]
    for i, point in enumerate(plan.get("proof_points", [])):
        target = _normalize(point.get("jd_requirement", ""))
        if not target:
            continue  # already caught by validate_plan_json
        if not any(target in r or r in target for r in requirements):
            _err(errors, f"proof_points[{i}].jd_requirement",
                 f"does not match any entry in jd_requirements: {point.get('jd_requirement', '')!r}")
    return errors
