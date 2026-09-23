import cover_letter_schema as cls

JD_TEXT = """
We need a senior designer who can lead design for AI features end to end and who has
built a design system that engineering actually adopted. You've managed designers
formally before, not just mentored them.
"""

CV_TEXT = """
## What I lead

- Led design for AI features on a global wealth platform: search, summarisation, decision support.
- Built a 700+ component system from scratch across 7 products and drove adoption to ~75%.
- Ran a year-long research programme with 240+ expert participants.
"""

GOOD_PLAN = {
    "opening_angle": "I want to trade organisation-building for hands-on craft again.",
    "jd_requirements": [
        "lead design for AI features end to end",
        "built a design system that engineering actually adopted",
    ],
    "jd_company_facts": [],
    "company_evidence": [],
    "proof_points": [
        {"claim": "I ship AI features end to end.",
         "jd_requirement": "lead design for AI features end to end",
         "cv_source_line": "Led design for AI features on a global wealth platform."},
        {"claim": "I build design systems from zero.",
         "jd_requirement": "built a design system that engineering actually adopted",
         "cv_source_line": "Built a 700+ component system from scratch across 7 products."},
    ],
    "differentiation": "I prototype in code, not just Figma.",
    "target_words": 300,
}


def test_validate_plan_json_accepts_well_formed():
    assert cls.validate_plan_json(GOOD_PLAN) == []


def test_validate_plan_json_rejects_missing_fields():
    errors = cls.validate_plan_json({"opening_angle": "x"})
    assert errors
    assert any("jd_requirements" in e for e in errors)
    assert any("jd_company_facts" in e for e in errors)
    assert any("company_evidence" in e for e in errors)
    assert any("proof_points" in e for e in errors)
    assert any("differentiation" in e for e in errors)
    assert any("target_words" in e for e in errors)


def test_validate_plan_json_accepts_empty_company_evidence_and_jd_company_facts():
    """Both are legitimately empty when there's nothing real to put there,
    same convention for both fields; never itself an error."""
    plan = {**GOOD_PLAN, "company_evidence": [], "jd_company_facts": []}
    assert cls.validate_plan_json(plan) == []


def test_validate_plan_json_rejects_jd_requirements_not_a_list():
    plan = {**GOOD_PLAN, "jd_requirements": "leads AI features"}
    errors = cls.validate_plan_json(plan)
    assert any("jd_requirements" in e for e in errors)


def test_validate_plan_json_respects_the_configured_proof_point_range():
    plan_with_one = {**GOOD_PLAN, "proof_points": GOOD_PLAN["proof_points"][:1]}
    assert cls.validate_plan_json(plan_with_one) != []  # 1 < default min of 2
    assert cls.validate_plan_json(plan_with_one, min_points=1, max_points=3) == []


def test_validate_plan_json_allows_three_proof_points_within_default_range():
    third = {"claim": "x", "jd_requirement": "built a design system that engineering actually adopted",
             "cv_source_line": "Ran a year-long research programme with 240+ expert participants."}
    plan = {**GOOD_PLAN, "proof_points": GOOD_PLAN["proof_points"] + [third]}
    assert cls.validate_plan_json(plan) == []  # default max is 3


def test_validate_plan_json_rejects_a_proof_point_missing_jd_requirement():
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "cv_source_line": "b"}, GOOD_PLAN["proof_points"][1],
    ]}
    errors = cls.validate_plan_json(plan)
    assert any("proof_points[0].jd_requirement" in e for e in errors)


def test_validate_plan_json_rejects_non_integer_target_words():
    plan = {**GOOD_PLAN, "target_words": "300"}
    errors = cls.validate_plan_json(plan)
    assert any("target_words" in e for e in errors)


def test_validate_plan_json_rejects_bool_as_target_words():
    """isinstance(True, int) is True in Python, a real footgun for a plain
    isinstance(v, int) check, since a model returning a JSON boolean would
    silently pass as a target word count of 0 or 1 without this guard."""
    plan = {**GOOD_PLAN, "target_words": True}
    errors = cls.validate_plan_json(plan)
    assert any("target_words" in e for e in errors)


# ------------------------------------------------------------------
# validate_source_lines (against the CV) — unchanged from pass 2
# ------------------------------------------------------------------

def test_validate_source_lines_passes_when_every_pointer_matches():
    assert cls.validate_source_lines(GOOD_PLAN, CV_TEXT) == []


def test_validate_source_lines_catches_an_invented_pointer():
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": GOOD_PLAN["jd_requirements"][0],
         "cv_source_line": "Ran the entire company's rebrand single-handedly."},
        GOOD_PLAN["proof_points"][1],
    ]}
    errors = cls.validate_source_lines(plan, CV_TEXT)
    assert len(errors) == 1
    assert "proof_points[0].cv_source_line" in errors[0]


def test_validate_source_lines_accepts_a_fragment_of_a_longer_bullet():
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": GOOD_PLAN["jd_requirements"][0],
         "cv_source_line": "Led design for AI features"},
        GOOD_PLAN["proof_points"][1],
    ]}
    assert cls.validate_source_lines(plan, CV_TEXT) == []


def test_validate_source_lines_checks_against_the_profile_paragraph_when_there_are_no_bullets():
    """Pass 4: a CV with only a "## Profile" paragraph and no bullets is
    still checkable against that paragraph's own sentences, not treated as
    having nothing to validate against — see extract_cv_lines."""
    cv = "## Profile\n\nI ship AI features end to end for expert users."
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": GOOD_PLAN["jd_requirements"][0],
         "cv_source_line": "I ship AI features end to end for expert users."},
        GOOD_PLAN["proof_points"][1],
    ]}
    errors = cls.validate_source_lines(plan, cv)
    assert any("proof_points[1]" in e for e in errors)  # the second pointer genuinely isn't in this CV
    assert not any("proof_points[0]" in e for e in errors)  # but the first one is


def test_validate_source_lines_errors_when_the_cv_has_nothing_at_all():
    errors = cls.validate_source_lines(GOOD_PLAN, "Just a stray sentence with no heading structure.")
    assert errors == ["cv_source_line: tailored CV has no bullet lines to validate against"]


# ------------------------------------------------------------------
# validate_jd_fields (against the JD) — new in pass 4
# ------------------------------------------------------------------

def test_validate_jd_fields_passes_when_everything_traces_to_the_jd():
    assert cls.validate_jd_fields(GOOD_PLAN, JD_TEXT) == []


def test_validate_jd_fields_catches_an_invented_requirement():
    plan = {**GOOD_PLAN, "jd_requirements": GOOD_PLAN["jd_requirements"] + ["knows how to fly a plane"]}
    errors = cls.validate_jd_fields(plan, JD_TEXT)
    assert len(errors) == 1
    assert "jd_requirements[2]" in errors[0]


def test_validate_jd_fields_catches_an_invented_company_fact():
    plan = {**GOOD_PLAN, "jd_company_facts": ["they just IPO'd on the moon"]}
    errors = cls.validate_jd_fields(plan, JD_TEXT)
    assert len(errors) == 1
    assert "jd_company_facts[0]" in errors[0]


def test_validate_jd_fields_accepts_a_fragment_of_a_longer_jd_sentence():
    plan = {**GOOD_PLAN, "jd_requirements": ["managed designers formally"]}
    assert cls.validate_jd_fields(plan, JD_TEXT) == []


def test_validate_jd_fields_tolerates_light_paraphrase_from_real_calibration_data():
    """Regression fixture from the pass-4 targeted run: an earlier
    exact-substring version of this check rejected six genuine, well-
    grounded jd_company_facts and one genuine jd_requirement from a real
    JD, every one confirmed by hand to actually be in the posting, because
    the model's extraction wasn't a character-exact substring ("Reports
    to" became "Reports directly to", "our product designer" became "the
    product designer", clauses got reordered). This is the shape of
    genuine extraction from prose, not fabrication, and must pass."""
    jd = ("...design authority for our Seven Bridges Platform...\n"
          "Reports to the Chief Strategy and Product Officer\n"
          "...you'll manage our product designer working on the clinical side "
          "of our portfolio...")
    plan = {**GOOD_PLAN, "jd_requirements": [
        "Reports directly to the Chief Strategy and Product Officer",
        "Will manage the product designer working on the clinical side of the portfolio",
    ], "jd_company_facts": ["Design authority for the Seven Bridges Platform"]}
    assert cls.validate_jd_fields(plan, jd) == []


def test_validate_jd_fields_still_rejects_genuine_fabrication_at_the_new_threshold():
    """The word-overlap fix must not become toothless: a claim sharing
    almost no vocabulary with the real JD still fails."""
    plan = {**GOOD_PLAN, "jd_requirements": ["knows how to fly a commercial airliner"]}
    errors = cls.validate_jd_fields(plan, JD_TEXT)
    assert len(errors) == 1
    assert "jd_requirements[0]" in errors[0]


def test_validate_jd_fields_errors_when_role_has_no_jd_text():
    errors = cls.validate_jd_fields(GOOD_PLAN, "")
    assert errors == ["jd_requirements: role has no JD text to validate against"]


# ------------------------------------------------------------------
# validate_proof_point_requirements (internal consistency) — new in pass 4
# ------------------------------------------------------------------

def test_validate_proof_point_requirements_passes_when_every_pointer_matches():
    assert cls.validate_proof_point_requirements(GOOD_PLAN) == []


def test_validate_proof_point_requirements_catches_an_unmatched_pointer():
    """The direct fix for the ablation's finding: a proof point whose
    jd_requirement doesn't name anything in the plan's own extracted list is
    exactly the "picked without checking against the JD" failure mode."""
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": "something never extracted as a requirement",
         "cv_source_line": GOOD_PLAN["proof_points"][0]["cv_source_line"]},
        GOOD_PLAN["proof_points"][1],
    ]}
    errors = cls.validate_proof_point_requirements(plan)
    assert len(errors) == 1
    assert "proof_points[0].jd_requirement" in errors[0]


def test_validate_proof_point_requirements_errors_when_jd_requirements_is_empty():
    plan = {**GOOD_PLAN, "jd_requirements": []}
    errors = cls.validate_proof_point_requirements(plan)
    assert errors == ["proof_points: jd_requirements is empty, so no proof point can trace to one"]


def test_validate_proof_point_requirements_accepts_a_fragment_match():
    plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": "lead design for AI features",
         "cv_source_line": GOOD_PLAN["proof_points"][0]["cv_source_line"]},
        GOOD_PLAN["proof_points"][1],
    ]}
    assert cls.validate_proof_point_requirements(plan) == []


# ------------------------------------------------------------------
# build_plan_tool / PLAN_TOOL shape
# ------------------------------------------------------------------

def test_plan_tool_schema_declares_the_expected_fields():
    props = cls.PLAN_TOOL["input_schema"]["properties"]
    expected = {"opening_angle", "jd_requirements", "jd_company_facts", "company_evidence",
                "proof_points", "differentiation", "target_words"}
    assert set(props) == expected
    assert set(cls.PLAN_TOOL["input_schema"]["required"]) == expected


def test_build_plan_tool_uses_the_given_proof_point_range():
    tool = cls.build_plan_tool(min_points=2, max_points=4)
    pp = tool["input_schema"]["properties"]["proof_points"]
    assert pp["minItems"] == 2
    assert pp["maxItems"] == 4


def test_build_plan_tool_defaults_match_the_module_constants():
    tool = cls.build_plan_tool()
    pp = tool["input_schema"]["properties"]["proof_points"]
    assert pp["minItems"] == cls.DEFAULT_MIN_PROOF_POINTS
    assert pp["maxItems"] == cls.DEFAULT_MAX_PROOF_POINTS


def test_proof_point_items_require_the_jd_requirement_field():
    tool = cls.build_plan_tool()
    item_props = tool["input_schema"]["properties"]["proof_points"]["items"]["properties"]
    assert set(item_props) == {"claim", "jd_requirement", "cv_source_line"}
    required = tool["input_schema"]["properties"]["proof_points"]["items"]["required"]
    assert set(required) == {"claim", "jd_requirement", "cv_source_line"}
