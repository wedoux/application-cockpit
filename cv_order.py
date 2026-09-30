#!/usr/bin/env python3
"""
Experience order: the timeline is fixed, tailoring happens inside it.
======================================================================
A recruiter reads a CV by its timeline, and an ATS reconstructs career
history from it. Shuffling past roles so the most relevant one leads
confuses both — the reader loses the shape of the career, and the parser
can mis-derive dates and gaps from an order it assumes is chronological.

So generation never reorders, adds, drops or merges roles. Relevance is
carried by the sections built to carry it ("What I lead" / "How I help",
and the profile paragraph), and by bullet selection and wording inside each
role, where it costs the timeline nothing.

Found 2026-09-30: two master CV variants had a 2021-22 role sitting below a
2015-19 one, and the generated CV inherited the order. The master files were
fixed by hand. This module is the part that stops it coming back from the
generation side, and it checks both ends:

  * The master file itself must be reverse-chronological. If it isn't,
    generation is blocked — the fix belongs in the source, not in every
    draft that inherits it.
  * The generated experience array must be in the master's order. If the
    same roles came back shuffled, the order is restored in code, for zero
    tokens, and recorded. Regenerating for this would pay a model to redo
    something mechanical.

Nothing here judges content. A draft whose role SET differs from the master
is a fidelity problem, not an ordering one, and this module reports it
rather than papering over it by reordering what is left.
"""

import re

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# The section that holds employment history. "Selected engagements" is the
# advisory variant's name for the same thing. The AI-project section has
# ### headings too, which is exactly why this is section-scoped rather than
# a sweep of every ### in the file.
_EXPERIENCE_HEADINGS = ("experience", "selected engagements")

# A heading with no em dash is not a dated role. "### Earlier" is the one
# such heading that still belongs to the timeline: a prose summary of the
# oldest roles, carried as a trailing undated sentinel so a draft may render
# it as a final experience entry without looking like an invented role.
_EM_DASH = "—"
_EARLIER = "earlier"

_DATE_RE = re.compile(
    r"^\s*(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<year>\d{4})", re.IGNORECASE)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _company_from_heading(heading):
    """Both master formats, told apart structurally rather than by which
    file they came from:

        "Role — Company, City"   (head of design, senior IC)
        "Company — Role | City"  (advisory)

    The advisory form is the one with a pipe after the em dash. Keying off
    that, rather than off the section title, means a file renaming its
    section doesn't silently change how its roles are read.
    """
    if _EM_DASH not in heading:
        return None
    before, _, after = heading.partition(_EM_DASH)
    if "|" in after:
        return before.strip() or None
    return after.split(",", 1)[0].strip() or None


def parse_master_roles(master_cv_text):
    """Every role in the master CV's experience section, in file order.

    A dated role is {"company", "heading", "start", "earlier": False}.
    The "### Earlier" block, if present, comes last as
    {"company": None, "heading": "Earlier", "start": None, "earlier": True,
     "text": <its prose>} — the prose is kept because a draft that renders
    Earlier as an experience entry names companies inside it, and those have
    to be checkable against something.
    """
    roles, in_section, pending, earlier = [], False, None, None
    for line in (master_cv_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = _norm(stripped[3:]) in _EXPERIENCE_HEADINGS
            pending = earlier = None
            continue
        if not in_section:
            continue
        if stripped.startswith("### "):
            heading = stripped[4:].strip()
            pending = earlier = None
            if _norm(heading) == _EARLIER:
                earlier = {"company": None, "heading": heading, "start": None,
                           "earlier": True, "text": ""}
                roles.append(earlier)
                continue
            company = _company_from_heading(heading)
            if company:
                pending = {"company": company, "heading": heading,
                           "start": None, "earlier": False}
                roles.append(pending)
            continue
        if earlier is not None and stripped and not stripped.startswith("---"):
            earlier["text"] = (earlier["text"] + " " + stripped).strip()
            continue
        # The date line is the first non-empty line under the heading.
        if pending is not None and stripped:
            m = _DATE_RE.match(stripped)
            if m:
                month = _MONTHS.get(m.group("mon").lower())
                if month:
                    pending["start"] = (int(m.group("year")), month)
            pending = None
    return roles


class MasterOrderError(ValueError):
    """A master CV whose roles are not reverse-chronological. Raised rather
    than reported, because every draft generated from it would inherit the
    order and the fix belongs in the one file, not in each draft."""


def check_master_order(master_cv_text, master_cv_name):
    """Raise MasterOrderError if the master's roles are not newest-first.

    Entries with no date are skipped rather than guessed at: a null date is
    a legitimate, documented state in this codebase (see the CV prompt's
    dates rule), and inventing an ordering for one would be the same class
    of error as inventing the date."""
    dated = [r for r in parse_master_roles(master_cv_text)
             if r["start"] and not r.get("earlier")]
    for earlier, later in zip(dated, dated[1:]):
        if later["start"] > earlier["start"]:
            raise MasterOrderError(
                f"{master_cv_name} lists roles out of order: "
                f"\"{later['heading']}\" (starts {later['start'][1]:02d}/{later['start'][0]}) "
                f"comes after \"{earlier['heading']}\" "
                f"(starts {earlier['start'][1]:02d}/{earlier['start'][0]}), "
                "but a CV's experience must be reverse-chronological. Fix the "
                "master file — every draft generated from it inherits this."
            )


def draft_company(value):
    """The company a draft entry names, normalised the SAME way the master
    headings are: first comma-segment, so a city suffix is dropped.

    The model writes "Acme Corp, Geneva" into the company field while the
    master heading is parsed down to "Acme Corp". Normalising only one side
    made every role read as simultaneously missing and invented — the
    signature of a matching bug, and it fired on a correct CV the first time
    this check ran for real."""
    return _norm((value or "").split(",", 1)[0])


def _earlier_names(value):
    """The companies a draft's Earlier entry names. The model joins them
    with slashes ("A / B / C"); commas are accepted too."""
    return [p.strip() for p in re.split(r"[/,]", value or "") if p.strip()]


def check_earlier_contents(value, earlier_text):
    """Every company in a draft's Earlier entry must appear in the master's
    Earlier line, in the same order. Returns (unknown_names, out_of_order).

    Containment against the raw line rather than parsing it into a list.
    That line is free prose and varies by variant — one of them ends "for
    finance, automotive, and e-commerce brands", where a comma-splitting
    parser would happily decide "automotive" is a former employer and flag
    a correct CV. Containment can only fail to catch something, never
    invent something, and failing safe is the right direction for a check
    whose false positives are what this whole pass is fixing."""
    haystack = _norm(earlier_text)
    unknown, positions = [], []
    for name in _earlier_names(value):
        needle = _norm(name)
        if not needle:
            continue
        at = haystack.find(needle)
        if at < 0:
            unknown.append(name)
        else:
            positions.append(at)
    return unknown, positions != sorted(positions)


def compare_experience_order(experience, master_roles):
    """Compare a generated experience array against the master's role order.

    Returns one of:
      {"status": "ok"}
      {"status": "reordered", "order": [...]}   same roles, wrong order
      {"status": "mismatch", "reasons": [str, ...]}

    A permutation of the dated roles is mechanical and gets fixed. Anything
    about WHICH roles are present is a fidelity question and is reported,
    never quietly resolved by sorting whatever happens to be there.

    The Earlier block is a trailing undated sentinel. A draft may render it
    as a final entry, it is exempt from the chronology check, and it must
    come last — a summary of several old roles sitting in the middle of a
    dated sequence means the model misread the structure, which is worth
    seeing rather than silently sorting away.
    """
    dated = [r for r in master_roles if not r.get("earlier")]
    earlier = next((r for r in master_roles if r.get("earlier")), None)
    master_order = [_norm(r["company"]) for r in dated]
    entries = [(i, draft_company((e or {}).get("company")), e)
               for i, e in enumerate(experience or [])]

    if not master_order or not entries:
        return {"status": "ok"}

    matched = [(i, c, e) for i, c, e in entries if c in master_order]
    extra = [(i, c, e) for i, c, e in entries if c not in master_order]

    reasons = []
    present = {c for _, c, _ in matched}
    missing = [r["company"] for r in dated if _norm(r["company"]) not in present]
    if missing:
        reasons.append("missing from the draft: " + ", ".join(missing))
    if len(present) != len(matched):
        reasons.append("the same role appears more than once")

    for i, _, e in extra:
        raw = (e or {}).get("company") or "(unnamed)"
        if earlier is None:
            reasons.append(f"not in the master CV: {raw}")
            continue
        unknown, out_of_order = check_earlier_contents(raw, earlier["text"])
        if unknown:
            reasons.append("not in the master CV's Earlier line: " + ", ".join(unknown))
        if out_of_order:
            reasons.append(f"the Earlier entry lists its companies out of order: {raw}")
        if i < max((j for j, _, _ in matched), default=-1):
            reasons.append("the Earlier summary is not last")

    if reasons:
        return {"status": "mismatch", "reasons": reasons}

    if [c for _, c, _ in matched] == [m for m in master_order if m in present]:
        return {"status": "ok"}
    return {"status": "reordered",
            "order": [r["company"] for r in dated if _norm(r["company"]) in present]}


def restore_order(experience, master_roles):
    """The generated entries, in the master CV's order. Content untouched —
    only the sequence changes. An entry matching no dated role (the Earlier
    summary) sorts last, which is where the master puts it."""
    dated = [r for r in master_roles if not r.get("earlier")]
    position = {_norm(r["company"]): i for i, r in enumerate(dated)}
    return sorted(experience,
                  key=lambda e: position.get(draft_company((e or {}).get("company")),
                                             len(position)))
