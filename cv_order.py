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

# A heading with no em dash is not a role: "### Earlier" is a summary block.
_EM_DASH = "—"

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
    """Every role in the master CV's experience section, in file order, as
    {"company", "heading", "start"}. start is (year, month) or None when the
    entry states no date."""
    roles, in_section, pending = [], False, None
    for line in (master_cv_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = _norm(stripped[3:]) in _EXPERIENCE_HEADINGS
            pending = None
            continue
        if not in_section:
            continue
        if stripped.startswith("### "):
            heading = stripped[4:].strip()
            company = _company_from_heading(heading)
            pending = None
            if company:
                pending = {"company": company, "heading": heading, "start": None}
                roles.append(pending)
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
    dated = [r for r in parse_master_roles(master_cv_text) if r["start"]]
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


def compare_experience_order(experience, master_roles):
    """Compare a generated experience array against the master's role order.

    Returns one of:
      {"status": "ok"}
      {"status": "reordered", "order": [...]}   same roles, wrong order
      {"status": "mismatch", "added": [...], "dropped": [...]}

    A reorder is mechanical and gets fixed. A mismatch is a fidelity
    question — which roles belong in this CV — and is reported, never
    quietly resolved by reordering whatever happens to be there.
    """
    master_order = [_norm(r["company"]) for r in master_roles]
    drafted = [_norm((e or {}).get("company")) for e in (experience or [])]

    if not master_order or not drafted:
        return {"status": "ok"}

    added = [d for d in drafted if d not in master_order]
    dropped = [m for m in master_order if m not in drafted]
    if added or dropped or len(drafted) != len(set(drafted)):
        return {
            "status": "mismatch",
            "added": [e.get("company") for e in experience
                      if _norm(e.get("company")) in added],
            "dropped": [r["company"] for r in master_roles
                        if _norm(r["company"]) in dropped],
        }

    if drafted == [m for m in master_order if m in drafted]:
        return {"status": "ok"}
    return {"status": "reordered", "order": [r["company"] for r in master_roles
                                             if _norm(r["company"]) in drafted]}


def restore_order(experience, master_roles):
    """The generated entries, in the master CV's order. Content untouched —
    only the sequence changes."""
    position = {_norm(r["company"]): i for i, r in enumerate(master_roles)}
    return sorted(experience, key=lambda e: position.get(_norm((e or {}).get("company")), len(position)))
