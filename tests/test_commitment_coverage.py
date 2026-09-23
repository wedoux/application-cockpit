"""
style_gate.scan_commitment_coverage — did the written letter deliver what
its own validated plan committed to?

Same convention as test_style_gate.py: this file tests the check's own logic,
not its wiring into generation.py's retry loop.

Two properties matter more than the rest, and both have a named test here
because both were arrived at by measurement rather than by design:

1. Numbers are read from cv_source_line as well as claim
   (test_numbers_are_read_from_the_source_line_not_only_the_claim). In both
   real failures this check was built for, the missing digits existed only
   in the source line while the claim said "at scale". A version reading
   claims alone passed both letters that had dropped the evidence.

2. Similarity never drives a miss
   (test_similarity_never_drives_a_miss). Twelve hand-labelled proof points
   showed no text-overlap measure separates a delivered commitment from a
   dropped one, and cosine actively inverts on the clearest pair. The scores
   are reported; they must never gate. A refactor that folds them back into
   misses re-breaks the check in the exact way the measurement ruled out.

All fixture companies and figures are invented, per the sample-profile
convention: nothing here is real experience.
"""

import style_gate as sg


def _plan(*points):
    return {"proof_points": list(points)}


def _point(claim, requirement="Lead a design team", source_line="Some CV bullet."):
    return {"claim": claim, "jd_requirement": requirement, "cv_source_line": source_line}


# ------------------------------------------------------------------
# The gate: committed figures reaching the prose
# ------------------------------------------------------------------

def test_committed_figure_present_is_covered():
    plan = _plan(_point(
        "Managed a design team at real scale",
        source_line="Managed and coached a team of 14 designers in the first year.",
    ))
    letter = ("I have managed formally rather than informally: 14 designers at "
              "Initech, restructured inside a year.")
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["covered"] is True
    assert res["misses"] == []
    assert res["per_proof_point"][0]["numbers_found"] == ["14"]


def test_committed_figure_absent_is_flagged_and_names_the_figure():
    plan = _plan(_point(
        "Managed a design team at real scale",
        source_line="Managed and coached a team of 14 designers in the first year.",
    ))
    letter = ("I have led design work across several products and cared a great "
              "deal about the craft of it.")
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["covered"] is False
    assert len(res["misses"]) == 1
    assert "14" in res["misses"][0]
    assert res["per_proof_point"][0]["numbers_missing"] == ["14"]


def test_numbers_are_read_from_the_source_line_not_only_the_claim():
    """The regression that defines this check. The claim carries no digits at
    all; every figure lives in the source line. Reading claims alone passes
    this letter, which is the observed failure."""
    plan = _plan(_point(
        "Runs discovery with users at scale",  # no digits anywhere
        source_line="Ran the research programme for a year: 9 studies with 180+ participants.",
    ))
    letter = "I have run discovery with expert users for most of my career, continuously."
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["per_proof_point"][0]["numbers"] == ["9", "180"]
    assert res["covered"] is False


def test_one_figure_landing_is_enough():
    """A source line carrying three figures is kept by a letter that cites
    one of them. Demanding all three fails letters whose evidence arrived."""
    plan = _plan(_point(
        "Built a design system engineering adopted",
        source_line="Built the first design system: 600+ components across 8 products, "
                    "cutting build time by ~40%.",
    ))
    letter = ("At Globex I built the first design system from nothing, 600+ components, "
              "and engineering built on it rather than around it.")
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["covered"] is True
    assert res["per_proof_point"][0]["numbers_found"] == ["600"]
    assert res["per_proof_point"][0]["numbers_missing"] == ["8", "40"]


def test_years_are_not_treated_as_commitments():
    """A date on a CV bullet is not the evidence the point rests on, and a
    retry chasing it would be noise."""
    plan = _plan(_point(
        "Led design for a regulated assistant",
        source_line="Design lead for the Acme assistant, launched globally in August 2024.",
    ))
    letter = "I led design for a regulated assistant built with guardrails against hallucination."
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["per_proof_point"][0]["numbers"] == []
    assert res["covered"] is True


def test_digit_boundaries_are_respected():
    """14 must not be found inside 140 or 2014."""
    plan = _plan(_point("Managed a team", source_line="Managed 14 designers."))
    res = sg.scan_commitment_coverage(
        "We shipped 140 components in 2014 to Acme.", plan)
    assert res["covered"] is False
    assert res["per_proof_point"][0]["numbers_missing"] == ["14"]


def test_thousands_separators_match_either_way():
    plan = _plan(_point("Scaled the platform", source_line="Served 1,200 advisors daily."))
    assert sg.scan_commitment_coverage("The platform served 1200 advisors.", plan)["covered"]
    assert sg.scan_commitment_coverage("The platform served 1,200 advisors.", plan)["covered"]


# ------------------------------------------------------------------
# Similarity is reported, never gated
# ------------------------------------------------------------------

def test_similarity_never_drives_a_miss():
    """A claim sharing almost no vocabulary with the letter still passes, as
    long as its committed figure landed. Twelve labelled points showed
    overlap cannot tell delivered from dropped; gating on it would fire on
    good letters at about the rate it fires on bad ones."""
    plan = _plan(_point(
        "Facilitated cross-functional alignment workshops for procurement stakeholders",
        requirement="Run workshops with procurement",
        source_line="Ran 6 alignment workshops.",
    ))
    letter = "Six? No. I counted 6 of them, and they changed how the team shipped."
    res = sg.scan_commitment_coverage(letter, plan)
    assert res["covered"] is True
    assert res["misses"] == []
    # low overlap is still reported, just not as a miss
    assert res["per_proof_point"][0]["claim_similarity"] < 0.2
    assert res["similarity_flags"]


def test_similarity_flags_are_separate_from_misses():
    plan = _plan(_point("Wholly unrelated vocabulary about maritime logistics",
                        source_line="No figures here."))
    res = sg.scan_commitment_coverage("A letter about design systems and research.", plan)
    assert res["misses"] == []
    assert res["covered"] is True
    assert len(res["similarity_flags"]) >= 1


# ------------------------------------------------------------------
# Honesty about what the check can and cannot see
# ------------------------------------------------------------------

def test_a_plan_with_no_figures_reports_nothing_checkable():
    """covered=True alongside checkable_points=0 means "checked for nothing",
    and both are reported so the first can't be read as a pass on its own."""
    plan = _plan(
        _point("Led design for an assistant", source_line="Design lead for the assistant."),
        _point("Worked closely with engineers", source_line="Paired with engineering daily."),
    )
    res = sg.scan_commitment_coverage("A letter making both of those arguments.", plan)
    assert res["covered"] is True
    assert res["checkable_points"] == 0
    assert res["proof_points_total"] == 2


def test_partial_checkability_is_counted():
    plan = _plan(
        _point("Led design", source_line="Design lead for the assistant."),
        _point("Managed a team", source_line="Managed 14 designers."),
    )
    res = sg.scan_commitment_coverage("I managed 14 designers.", plan)
    assert res["checkable_points"] == 1
    assert res["proof_points_total"] == 2


def test_no_plan_returns_none_not_a_pass():
    """The freeform path has no plan. None and "nothing missing" are
    different facts and callers must be able to tell them apart."""
    assert sg.scan_commitment_coverage("Any letter.", None) is None
    assert sg.scan_commitment_coverage("Any letter.", {}) is None
    assert sg.scan_commitment_coverage("Any letter.", {"proof_points": []}) is None


def test_misses_name_the_point_so_a_retry_can_act_on_them():
    plan = _plan(
        _point("First point", source_line="No numbers."),
        _point("Second point", source_line="Managed 14 designers."),
    )
    res = sg.scan_commitment_coverage("A letter with no figures at all.", plan)
    assert len(res["misses"]) == 1
    assert "proof point 2" in res["misses"][0]


# ------------------------------------------------------------------
# Spelled-out numerals
#
# The false positive that nearly decided pass 5 the wrong way: the write
# step delivered a management commitment in 5 runs of 5, and a
# digits-only check scored it 3, because two letters wrote "a team of
# eighteen designers" instead of "18". A check that calls a kept promise
# broken is worse than no check, because it drives a retry against a letter
# that was already right.
# ------------------------------------------------------------------

def test_spelled_out_numeral_counts_as_delivered():
    plan = _plan(_point("Managed a team",
                        source_line="Managed and coached a team of 18 designers."))
    letter = "I coached and managed a team of eighteen designers within my first year."
    assert sg.scan_commitment_coverage(letter, plan)["covered"] is True


def test_spelled_out_compound_numerals():
    for value, written in [
        ("52", "fifty-two"), ("52", "fifty two"), ("75", "seventy-five"),
        ("17", "seventeen"), ("7", "seven"), ("240", "two hundred and forty"),
        ("240", "two hundred forty"), ("700", "seven hundred"),
        ("3000", "three thousand"),
    ]:
        plan = _plan(_point("A claim", source_line=f"Delivered {value} of them."))
        res = sg.scan_commitment_coverage(f"We delivered {written} of them.", plan)
        assert res["covered"] is True, f"{value} written as {written!r} was not found"


def test_seven_does_not_match_inside_seventeen():
    plan = _plan(_point("A claim", source_line="Across 7 products."))
    res = sg.scan_commitment_coverage("It ran for seventeen months.", plan)
    assert res["covered"] is False


def test_digits_still_win_when_present():
    plan = _plan(_point("Managed a team", source_line="Managed 18 designers."))
    assert sg.scan_commitment_coverage("I managed 18 designers.", plan)["covered"] is True


def test_a_figure_delivered_only_as_a_description_is_still_scored_missing():
    """Documented limitation, pinned so it changes deliberately: "roughly
    half" keeps a commitment to 52% for any reader, and this check cannot
    see it. The one-figure-is-enough rule is what stops this sinking a
    proof point that carries other figures."""
    plan = _plan(_point("Cut build time",
                        source_line="Cutting front-end build time by ~52%."))
    res = sg.scan_commitment_coverage("It cut front-end build time by roughly half.", plan)
    assert res["covered"] is False
