import pytest

import numeric_fact_gate as ng


def test_extracts_plain_numbers_percentages_and_years():
    text = "Cut cost 52% in 2024 while leading 7 designers."
    tokens = ng.extract_numeric_tokens(text)
    assert {"52%", "2024", "7"} <= tokens


def test_comma_grouped_and_ungrouped_numbers_normalize_equal():
    assert ng.normalize_number("16,181") == ng.normalize_number("16181") == "16181"


def test_decimal_is_not_treated_as_grouping():
    # A genuine decimal must survive — only a comma/period followed by
    # EXACTLY three digits is thousands-grouping.
    assert ng.normalize_number("2.5") == "2.5"


def test_magnitude_suffix_is_part_of_the_token():
    assert ng.normalize_number("700+") == "700+"
    assert ng.normalize_number("$2T") == "$2t"


def test_spelled_out_magnitude_folds_to_the_same_token_as_the_abbreviation():
    # Real surface variant found calibrating against a generated draft: the
    # master CV says "$2T", the model wrote "$2 trillion" — same fact.
    tokens = ng.extract_numeric_tokens("administering over $2 trillion in assets")
    assert "$2t" in tokens


def test_magnitude_suffix_does_not_swallow_an_unrelated_unit():
    # "50kg" must not read as "50k" — the suffix has to END the token.
    tokens = ng.extract_numeric_tokens("Shipped 50kg servers")
    assert "50" in tokens
    assert "50k" not in tokens


def test_trailing_sentence_punctuation_is_not_part_of_the_number():
    # A regression: "...in August 2025," extracted the year WITH the comma,
    # so it never matched the same year written without one.
    tokens = ng.extract_numeric_tokens("Launched globally in August 2025, covering search.")
    assert "2025" in tokens
    assert "2025," not in tokens


def test_check_numeric_facts_passes_when_every_number_is_sourced():
    corpus = ng.build_corpus("Led a team of 7 designers.", "Looking for a design leader.", "")
    result = ng.check_numeric_facts("I led a team of 7 designers.", corpus)
    assert result["blocked"] is False
    assert result["unmatched"] == []
    assert result["checked"] == 1


def test_check_numeric_facts_blocks_an_unsourced_number():
    corpus = ng.build_corpus("Led a team of 7 designers.", "", "")
    result = ng.check_numeric_facts("I led a team of 27 designers.", corpus)
    assert result["blocked"] is True
    assert "27" in result["unmatched"]


def test_check_numeric_facts_draws_from_all_three_corpus_sources():
    master_cv, jd, profile = "Team of 7.", "Budget of $500k.", "Certified in 2020."
    corpus = ng.build_corpus(master_cv, jd, profile)
    result = ng.check_numeric_facts("Team of 7, $500k budget, certified 2020.", corpus)
    assert result["blocked"] is False


def test_real_draft_with_an_inflated_and_an_invented_number_is_caught():
    """Mirrors the retroactive calibration sanity check: inflating a real
    figure and inventing a new one must both surface as unmatched."""
    master_cv = "17 years of experience. HSBC: 74% improvement in satisfaction."
    draft = "27 years of experience. HSBC: 199% improvement in satisfaction."
    corpus = ng.build_corpus(master_cv, "", "")
    result = ng.check_numeric_facts(draft, corpus)
    assert result["blocked"] is True
    assert "27" in result["unmatched"]
    assert "199%" in result["unmatched"]


def test_draft_may_drop_the_plus_from_a_sourced_n_plus():
    """Regression, 2026-09-29: the CV says "700+ components", the
    JD says "35+ product designers", and the drafts wrote them as "over 700"
    and "35". Understating a sourced "N+" is the same fact, not an invention."""
    corpus = ng.build_corpus("700+ components across 7 products.",
                             "Our team of 35+ product designers.", "")
    for draft in ("over 700 components", "more than 700 components",
                  "~700 components", "a team of 35 designers",
                  "700+ components and 35+ designers"):
        result = ng.check_numeric_facts(draft, corpus)
        assert result["blocked"] is False, draft


def test_draft_may_not_add_a_plus_the_source_does_not_have():
    # "7 products" in the source; "7+ products" in the draft inflates it.
    corpus = ng.build_corpus("700+ components across 7 products.", "", "")
    result = ng.check_numeric_facts("across 7+ products", corpus)
    assert result["blocked"] is True
    assert "7+" in result["unmatched"]


def test_dropping_the_plus_does_not_launder_a_different_number():
    corpus = ng.build_corpus("700+ components.", "", "")
    result = ng.check_numeric_facts("800 components", corpus)
    assert result["blocked"] is True
    assert "800" in result["unmatched"]


# ---------------------------------------------------------------------------
# Rung 1 — deterministic equivalence. No API call; a number that is sourced
# under these rules simply passes, and says which rule let it through.
# ---------------------------------------------------------------------------

def test_real_case_over_700_components():
    """29 Sep 2026, blocked draft 1 of 3. Master CV: "700+ components"."""
    corpus = ng.build_corpus("Design system of 700+ components.", "", "")
    result = ng.check_numeric_facts("a design system of over 700 components", corpus)
    assert result["blocked"] is False
    assert result["equivalences"] == [{"rule": "n-plus", "draft": "700", "source": "700+"}]


def test_real_case_35_designers():
    """29 Sep 2026, blocked draft 2 of 3. JD: "35+ product designers"."""
    corpus = ng.build_corpus("", "a team of 35+ product designers", "")
    result = ng.check_numeric_facts("supporting 35 designers", corpus)
    assert result["blocked"] is False
    assert result["equivalences"] == [{"rule": "n-plus", "draft": "35", "source": "35+"}]


def test_real_case_canonical_cv_zero_to_three():
    """29 Sep 2026, blocked draft 3 of 3. cv-head-of-design.md says "from
    zero to a team of 3"; the draft wrote "from 0 to 3"."""
    corpus = ng.build_corpus("Grew the practice from zero to a team of 3.", "", "")
    result = ng.check_numeric_facts("took it from 0 to 3 designers", corpus)
    assert result["blocked"] is False
    assert result["equivalences"] == [{"rule": "number-word", "draft": "0", "source": "zero"}]


def test_number_words_are_checked_in_the_draft():
    """The hole going the other way: before this, "seven products" was never
    extracted, so an invented one was never checked."""
    corpus = ng.build_corpus("Shipped 3 products.", "", "")
    result = ng.check_numeric_facts("shipped seven products", corpus)
    assert result["blocked"] is True
    assert result["unmatched"] == ["7"]


def test_equivalence_works_in_both_directions():
    digits_source = ng.build_corpus("Led 12 studies.", "", "")
    assert ng.check_numeric_facts("led twelve studies", digits_source)["blocked"] is False
    words_source = ng.build_corpus("Led twelve studies.", "", "")
    assert ng.check_numeric_facts("led 12 studies", words_source)["blocked"] is False


def test_one_of_the_first_is_not_a_numeric_claim():
    """A false positive here blocks a draft that was correct, which is the
    failure this whole pass exists to reduce."""
    corpus = ng.build_corpus("Worked at a UK bank.", "", "")
    result = ng.check_numeric_facts("one of the first UK banks to ship it", corpus)
    assert result["blocked"] is False
    assert result["checked"] == 0


@pytest.mark.parametrize("phrase", [
    "no one owned the roadmap",
    "they briefed one another",
    "the one thing that mattered",
    "every one of the teams",
])
def test_non_quantifying_number_words_are_ignored(phrase):
    result = ng.check_numeric_facts(phrase, ng.build_corpus("nothing numeric", "", ""))
    assert result["blocked"] is False, phrase
    assert result["checked"] == 0, phrase


def test_a_number_word_at_the_end_of_a_sentence_is_left_alone():
    # Conservative on purpose: nothing follows, so it is not clearly a count.
    result = ng.check_numeric_facts("the team grew to seven", ng.build_corpus("x", "", ""))
    assert result["checked"] == 0


def test_compounds_are_read_whole_not_in_pieces():
    """"twenty-five designers" must not emit 20 and 5 — two numbers no
    corpus has, blocking a draft that was right."""
    assert ng.extract_numeric_tokens("twenty-five designers") == {"25"}
    assert ng.extract_numeric_tokens("seven hundred components") == {"700"}
    corpus = ng.build_corpus("A team of 25 designers and 700 components.", "", "")
    assert ng.check_numeric_facts("twenty-five designers, seven hundred components",
                                   corpus)["blocked"] is False


def test_seventeen_is_not_read_as_seven():
    assert ng.extract_numeric_tokens("seventeen years of experience") == {"17"}


def test_an_equivalence_is_reported_only_when_a_rule_was_needed():
    """A number written the same way on both sides is not a repair, and
    reporting it would bury the ones that matter in noise."""
    corpus = ng.build_corpus("Led a team of 7 designers.", "", "")
    result = ng.check_numeric_facts("led a team of 7 designers", corpus)
    assert result["equivalences"] == []
