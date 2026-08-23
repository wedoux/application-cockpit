import shutil

import pytest

import ats_verify as av

pytestmark = pytest.mark.skipif(
    shutil.which("pdftotext") is None,
    reason="poppler's pdftotext not installed",
)


def test_no_cid_or_replacement_chars_passes_clean_text():
    assert av.check_no_cid_or_replacement_chars("Jordan Reyes, 12 years.") == []


def test_cid_marker_is_caught():
    problems = av.check_no_cid_or_replacement_chars("hello (cid:12) world")
    assert problems


def test_replacement_char_is_caught():
    problems = av.check_no_cid_or_replacement_chars("broken glyph � here")
    assert problems


def test_contact_present_as_literal_text_passes():
    text = "Reach me at a@b.com or +41 79 000 00 00."
    assert av.check_contact_is_literal_text(text, email="a@b.com", phone="+41 79 000 00 00") == []


def test_contact_missing_is_caught():
    problems = av.check_contact_is_literal_text("no contact info here", email="a@b.com", phone="+41")
    assert len(problems) == 2


def test_reading_order_passes_when_landmarks_are_in_visual_order():
    text = "PROFILE\n...\nEXPERIENCE\n...\nLANGUAGES"
    assert av.check_reading_order(text, ["PROFILE", "EXPERIENCE", "LANGUAGES"]) == []


def test_reading_order_catches_a_scrambled_document():
    text = "LANGUAGES\n...\nPROFILE\n...\nEXPERIENCE"
    problems = av.check_reading_order(text, ["PROFILE", "EXPERIENCE", "LANGUAGES"])
    assert problems


def test_reading_order_skips_landmarks_the_document_type_doesnt_have():
    # A cover letter has no EXPERIENCE/EDUCATION headings — absence must not
    # itself be a failure, only an out-of-order landmark is.
    text = "PROFILE\n...\nLANGUAGES"
    assert av.check_reading_order(text, ["PROFILE", "EXPERIENCE", "LANGUAGES"]) == []


def test_no_internal_repetition_passes_unique_content():
    text = "This is one real sentence about a project. This is a different sentence entirely."
    assert av.check_no_internal_repetition(text) == []


def test_internal_repetition_catches_a_duplicated_sentence():
    sentence = "This sentence appears twice in the document for no good reason at all."
    text = f"{sentence} Some other text in between. {sentence}"
    problems = av.check_no_internal_repetition(text)
    assert problems


def test_posting_keywords_returns_none_without_a_jd():
    assert av.check_posting_keywords("some CV text", jd_text=None) is None


def test_posting_keywords_ignores_common_function_words():
    # Regression: "that" (7x in a CV vs 2x in its JD) is not a posting
    # keyword and must never be reported as stuffed.
    jd = "We need someone that cares about design. We know that this matters."
    cv = "that " * 7 + "design work"
    result = av.check_posting_keywords(cv, jd)
    assert result["stuffed"] == []


def test_posting_keywords_ratio_check_is_known_unreliable_on_dense_cvs():
    # This is a documented LIMITATION, not a pass/fail spec: a real design CV
    # saying "design" 19-35x against a JD's 2-9 mentions is normal density,
    # not stuffing, and this function's ratio heuristic can't tell the two
    # apart (it flagged both live before app.py's gate was fixed to ignore
    # "stuffed" as a blocking signal — see the comment there). Asserting the
    # actual (imperfect) behavior here so a future change to this heuristic
    # is a deliberate, visible decision, not a silent drift back into
    # blocking on it.
    jd = "Senior product design role. Design leadership. Design systems experience."
    cv = "design " * 25 + "work"
    result = av.check_posting_keywords(cv, jd)
    assert result["stuffed"] == [("design", 25, 3)]
