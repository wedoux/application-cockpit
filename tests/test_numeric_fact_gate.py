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
    assert result == {"blocked": False, "unmatched": [], "checked": 1}


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
