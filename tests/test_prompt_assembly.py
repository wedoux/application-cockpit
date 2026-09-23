"""
Category-conditional role framing (2026-09-22 fix).
=====================================================
build_role_frame used to inject one flat tagline_quote-driven line into
every generation regardless of category, and COVER_LETTER_TASK repeated a
hardcoded "lead with the strategic angle" instruction on top of it — wrong
for a Senior IC craft role, right only for Leadership. These tests cover the
new category -> framing_map selection and its fallback to tagline_quote.
"""

import prompt_assembly as pa


FRAMING_MAP = {
    "Leadership": "org-scale and platform-level product strategy.",
    "Senior IC": "hands-on craft, pairing with engineering, shipping end to end. "
                 "Do not lead with org scale, headcount, or design-system adoption percentages.",
}


def test_category_framing_wins_over_tagline_quote_when_both_present():
    frame = pa.build_role_frame(
        "Jordan Reyes", tagline_quote="A flat line that applies to everyone.",
        category="Senior IC", framing_map=FRAMING_MAP,
    )
    assert "hands-on craft" in frame
    assert "Do not lead with org scale" in frame
    assert "A flat line that applies to everyone." not in frame


def test_different_categories_get_different_framing():
    leadership = pa.build_role_frame("Jordan Reyes", category="Leadership", framing_map=FRAMING_MAP)
    senior_ic = pa.build_role_frame("Jordan Reyes", category="Senior IC", framing_map=FRAMING_MAP)
    assert "org-scale" in leadership
    assert "org-scale" not in senior_ic
    assert "hands-on craft" in senior_ic
    assert "hands-on craft" not in leadership


def test_falls_back_to_tagline_quote_when_category_has_no_framing_entry():
    frame = pa.build_role_frame(
        "Jordan Reyes", tagline_quote="Most companies think UX is buttons and colours.",
        category="Advisory", framing_map=FRAMING_MAP,  # no "Advisory" key in FRAMING_MAP
    )
    assert "Most companies think UX is buttons and colours." in frame
    assert "Lead with UX-as-strategy" in frame


def test_falls_back_to_tagline_quote_when_no_category_given():
    frame = pa.build_role_frame("Jordan Reyes", tagline_quote="A flat line.", framing_map=FRAMING_MAP)
    assert "A flat line." in frame


def test_no_lead_line_at_all_when_neither_is_configured():
    frame = pa.build_role_frame("Jordan Reyes")
    assert "Lead with UX-as-strategy" not in frame
    assert "Framing for this role" not in frame


def test_build_system_prompt_threads_category_into_the_role_frame(tmp_path):
    writing_rules = tmp_path / "wr.md"
    about_me = tmp_path / "am.md"
    writing_rules.write_text("Writing rules content.")
    about_me.write_text("About me content.")
    cfg = {
        "paths": {"writing_rules": str(writing_rules), "about_me": str(about_me)},
        "company_logo_map": {},
        "profile": {"name": "Jordan Reyes", "framing": FRAMING_MAP},
    }
    system_senior_ic = pa.build_system_prompt(cfg, "master cv text", category="Senior IC")
    system_leadership = pa.build_system_prompt(cfg, "master cv text", category="Leadership")
    assert "hands-on craft" in system_senior_ic
    assert "org-scale" not in system_senior_ic
    assert "org-scale" in system_leadership


def test_cover_letter_task_no_longer_hardcodes_the_strategic_angle():
    assert "UX as strategy" not in pa.COVER_LETTER_TASK
    assert "role frame above" in pa.COVER_LETTER_TASK
