"""
style_gate.py — mechanical prose-style checks, advisory only.

Same convention as test_numeric_fact_gate.py: this file tests the gate's own
logic (does each regex catch what it should and leave what it shouldn't),
not its wiring into app.py (see test_verify_gates.py for wiring tests, once
it has one for this gate).

Every negative-parallelism sub-shape gets one positive fixture (it catches
the real thing) and one negative fixture (it doesn't fire on ordinary prose
that happens to contain "not" or "and"). The copula-negation case
(test_copula_negation_restate_catches_the_missed_linear_sentence) is the
specific regression this gate exists to close — see style_gate.py's module
docstring and docs/cover-letter-quality-findings.md §4: a first pass that
only matched the literal word "not" missed "wasn't X, it was Y" entirely,
because "wasn't" doesn't contain "not" as a word.
"""

import style_gate as sg


# ------------------------------------------------------------------
# Negative parallelism
# ------------------------------------------------------------------

def test_comma_not_catches_the_shape():
    r = sg.scan_negative_parallelism("Quality is a strategy, not a finishing touch.")
    assert r["comma_not"]["count"] == 1


def test_comma_not_does_not_fire_on_plain_negation_without_a_preceding_comma():
    r = sg.scan_negative_parallelism("I have not worked in that industry before.")
    assert r["comma_not"]["count"] == 0


def test_comma_not_just_is_counted_separately_from_comma_not():
    r = sg.scan_negative_parallelism("I've been building agentic systems myself, not just designing around them.")
    assert r["comma_not_just"]["count"] == 1
    assert r["comma_not"]["count"] == 0, "a 'not just' match must not double-count as a plain comma_not"


def test_copula_negation_restate_catches_the_missed_real_sentence():
    """The regression fixture. This exact sentence from a real stored
    letter was missed by an earlier
    pattern that only matched the literal word "not" — "wasn't" is a
    contraction, not that word, and this is the purest instance of the
    negative-parallelism tell in that letter."""
    text = ("The hard problem there wasn't the AI, it was how you signal confidence "
            "in a machine judgment to someone about to make a consequential financial "
            "decision on the strength of it.")
    r = sg.scan_negative_parallelism(text)
    assert r["copula_negation_restate"]["count"] == 1
    assert r["comma_not"]["count"] == 0, "wasn't/isn't must not also double-count under comma_not"


def test_copula_negation_restate_catches_isnt_and_its_variant():
    r = sg.scan_negative_parallelism("It isn't a redesign, it's a rebuild from scratch.")
    assert r["copula_negation_restate"]["count"] == 1


def test_copula_negation_restate_does_not_fire_on_an_unrelated_wasnt():
    r = sg.scan_negative_parallelism("The first version wasn't great, but the team learned fast and shipped a solid v2.")
    assert r["copula_negation_restate"]["count"] == 0


def test_not_just_sentence_initial_catches_a_leading_not_just():
    r = sg.scan_negative_parallelism("Ship fast. Not just quickly, but with real craft behind every release.")
    assert r["not_just_sentence_initial"]["count"] == 1


def test_not_just_sentence_initial_does_not_fire_mid_sentence():
    r = sg.scan_negative_parallelism("I care about doing this well, not just doing it fast.")
    assert r["not_just_sentence_initial"]["count"] == 0


def test_not_about_pair_catches_the_shape():
    r = sg.scan_negative_parallelism("It's not about the tool, it's about the judgment behind using it.")
    assert r["not_about_pair"]["count"] == 1


def test_not_about_pair_does_not_fire_on_a_single_not_about():
    r = sg.scan_negative_parallelism("This isn't about credentials. It's about the work.")
    assert r["not_about_pair"]["count"] == 0


def test_not_only_but_also_catches_the_shape():
    r = sg.scan_negative_parallelism("Not only did we ship on time, but also under budget.")
    assert r["not_only_but_also"]["count"] == 1


def test_not_only_but_also_does_not_fire_without_but_also():
    r = sg.scan_negative_parallelism("Not only did we ship on time this quarter.")
    assert r["not_only_but_also"]["count"] == 0


def test_rather_than_instead_of_are_counted_but_excluded_from_total_strict():
    r = sg.scan_negative_parallelism("I want hands-on craft rather than management, and I'd rather ship instead of plan forever.")
    assert r["rather_than_instead_of"]["count"] == 2
    assert r["total_strict"] == 0, "rather than / instead of must never count toward the strict total"


def test_total_strict_sums_every_strict_shape_once_each():
    text = ("Quality is a strategy, not a finishing touch. "
            "The hard problem wasn't the AI, it was the confidence signal. "
            "Not just fast, but durable. "
            "It's not about speed, it's about judgment. "
            "Not only did it ship, but also on budget.")
    r = sg.scan_negative_parallelism(text)
    assert r["total_strict"] == 5


def test_real_linear_letter_matches_the_verified_recount():
    """documents.id=36's stored content, pasted verbatim (not re-fetched from
    a live DB, so this test has no dependency on cockpit.db existing). An
    exhaustive manual scan of the letter for \\bnot\\b and for wasn't/isn't
    found exactly three negation instances in the whole 583-word letter —
    two literal "not" (one comma_not, one comma_not_just) and one "wasn't"
    copula. total_strict must equal that, not four: a "two strict X, not Y"
    plus a separately-counted "not just X" double-counts the same
    "not just designing around them" span once as a comma-not-just match
    and once again as if it were a distinct sentence-initial occurrence,
    which this letter does not actually contain."""
    text = (
        "Globex's bet is that the interface for building software has to change as much "
        "as the software itself does, and that quality is a strategy, not a finishing touch. "
        "That's the same argument I've been making for seventeen years in fintech, where the "
        "temptation is always to treat UX as decoration on top of \"real\" engineering work. "
        "It never is. It's the thing that decides whether an expert trusts what the system is "
        "telling them, and whether they act on it.\n\n"
        "The hard problem there wasn't the AI, it was how you signal confidence in a machine "
        "judgment to someone about to make a consequential financial decision on the strength "
        "of it.\n\n"
        "I've also been building agentic systems myself, not just designing around them."
    )
    r = sg.scan_negative_parallelism(text)
    assert r["comma_not"]["count"] == 1
    assert r["comma_not_just"]["count"] == 1
    assert r["copula_negation_restate"]["count"] == 1
    assert r["not_just_sentence_initial"]["count"] == 0
    assert r["total_strict"] == 3


# ------------------------------------------------------------------
# Vocabulary
# ------------------------------------------------------------------

def test_cursed_word_is_caught():
    r = sg.scan_vocabulary("Our platform delivers a seamless, robust experience.")
    assert set(r["cursed_words"]["matches"]) == {"seamless", "robust"}


def test_cursed_word_list_does_not_fire_on_unrelated_prose():
    r = sg.scan_vocabulary("I shipped a redesign that cut handling time by a third.")
    assert r["cursed_words"]["count"] == 0


def test_stock_phrase_is_caught():
    r = sg.scan_vocabulary("When it comes to design systems, consistency matters most.")
    assert "when it comes to" in r["stock_phrases"]["matches"]


# ------------------------------------------------------------------
# Editorializing / conclusion reflex
# ------------------------------------------------------------------

def test_editorializing_signpost_is_caught():
    r = sg.scan_editorializing("Notably, the redesign shipped two weeks early.")
    assert "notably" in r["matches"]


def test_conclusion_reflex_is_caught():
    r = sg.scan_conclusion_reflex("Overall, this role fits my background well.")
    assert "overall," in r["matches"]


def test_conclusion_reflex_does_not_fire_on_ordinary_prose():
    r = sg.scan_conclusion_reflex("I led the redesign end to end, from research to launch.")
    assert r["count"] == 0


# ------------------------------------------------------------------
# False ranges (informational only — see docstring for why no strict count)
# ------------------------------------------------------------------

def test_false_range_shape_is_detected():
    r = sg.scan_false_ranges("From intimate gatherings to global movements, the platform scales.")
    assert r["count"] == 1


# ------------------------------------------------------------------
# Triplets
# ------------------------------------------------------------------

def test_triplet_is_caught():
    r = sg.scan_triplets("The work was innovative, transformative, and groundbreaking.")
    assert r["total"] == 1


def test_two_item_and_is_not_mistaken_for_a_triplet():
    """Regression: an early version of this regex matched straight through a
    plain two-item conjunction that happened to follow an unrelated comma
    earlier in the sentence — see style_gate.py's _TRIPLET_RE comment."""
    r = sg.scan_triplets("I worked with product and engineering leadership on this, and it went well.")
    assert r["total"] == 0


def test_triplet_count_is_per_paragraph():
    text = "Innovative, transformative, and groundbreaking.\n\nA second paragraph with no list at all."
    r = sg.scan_triplets(text)
    assert r["by_paragraph"] == [1, 0]


# ------------------------------------------------------------------
# Em dashes / word count
# ------------------------------------------------------------------

def test_em_dash_is_counted():
    assert sg.scan_em_dashes("A sentence — with an em dash — in it.")["count"] == 2


def test_em_dash_count_is_zero_when_absent():
    assert sg.scan_em_dashes("A sentence with no dashes in it.")["count"] == 0


def test_word_count_flags_over_ceiling():
    r = sg.scan_word_count(" ".join(["word"] * 400), ceiling=300)
    assert r["count"] == 400
    assert r["over_ceiling"] is True


def test_word_count_passes_under_ceiling():
    r = sg.scan_word_count(" ".join(["word"] * 200), ceiling=300)
    assert r["over_ceiling"] is False


# ------------------------------------------------------------------
# extract_cv_lines (implementation pass 4 extended this)
# ------------------------------------------------------------------

def test_extract_cv_lines_gets_plain_bullets():
    cv = "## What I lead\n\n- First bullet here.\n- Second bullet here.\n"
    assert sg.extract_cv_lines(cv) == ["First bullet here.", "Second bullet here."]


def test_extract_cv_lines_includes_profile_paragraph_sentences():
    """Regression fixture: a real generation cited "17 years building
    information-dense, expert-user products in finance and RegTech" as a
    cv_source_line, and it genuinely lives only in the CV's "## Profile"
    paragraph, not in any bullet — a check that only recognized bullets
    rejected a real, correctly-sourced claim."""
    cv = ("## Profile\n\nI design software, not decks. 17 years building "
          "information-dense, expert-user products in finance and RegTech.\n\n"
          "## What I lead\n\n- A bullet that should also still be found.\n")
    lines = sg.extract_cv_lines(cv)
    assert any("17 years building" in l for l in lines)
    assert "A bullet that should also still be found." in lines


def test_extract_cv_lines_stops_the_profile_paragraph_at_the_next_heading():
    cv = "## Profile\n\nProfile text here.\n\n## Experience\n\nThis must not be included.\n"
    lines = sg.extract_cv_lines(cv)
    assert any("Profile text here" in l for l in lines)
    assert not any("must not be included" in l for l in lines)


def test_extract_cv_lines_returns_empty_when_the_cv_has_neither():
    assert sg.extract_cv_lines("Just a stray sentence with no heading structure.") == []


def test_extract_cv_lines_handles_no_profile_heading_at_all():
    cv = "## Experience\n\n- A bullet with no profile section present.\n"
    assert sg.extract_cv_lines(cv) == ["A bullet with no profile section present."]


# ------------------------------------------------------------------
# Duplication proxy
# ------------------------------------------------------------------

def test_duplication_is_none_when_no_cv_text_given():
    assert sg.scan_duplication("Some letter text.", None) is None


def test_duplication_catches_a_near_verbatim_five_gram():
    cv = "Built the company's first design system from scratch across seven products."
    letter = "I built the company's first design system from scratch, which was a big lift."
    r = sg.scan_duplication(letter, cv)
    assert r["shared_ngrams"] >= 1
    assert r["overlap_pct"] > 0


def test_duplication_is_zero_for_genuinely_different_text():
    cv = "Led the AML redesign at Acme Corp across seven products."
    letter = "I'm drawn to your small autonomous teams and no-handoff shipping model."
    r = sg.scan_duplication(letter, cv)
    assert r["shared_ngrams"] == 0
    assert r["overlap_pct"] == 0.0


# ------------------------------------------------------------------
# Paraphrase-aware duplication (scan_paraphrase_duplication). The fixture
# below is fictional (Jordan-Reyes-style — no real employer, no real
# person), built to the same shape as a real letter that was calibrated
# against by hand: one opening paragraph, three paragraphs that each
# reword a specific CV bullet into different words (no shared 5-grams —
# scan_duplication's proxy would miss all three), and one closing
# paragraph. The real calibration — this metric run against the actual
# letter and CV that motivated it (documents.id 36/35 in cockpit.db,
# findings doc §diagnosis) — separated its four restated paragraphs
# (0.34-0.52) from its two original ones (0.17-0.20) with a clean gap, and
# a 0.3 threshold reproduced "four of six paragraphs restate the CV"
# exactly, independent of the human read that first named that count. That
# real data can't live in a committed test (it's a real employer's name
# and a real person's career history), so this fixture proves the same
# property — a clean gap between restated and original paragraphs, at the
# same 0.3 threshold — on invented content of the same shape.
# ------------------------------------------------------------------

_FAKE_CV = """
- Built the fraud-detection design system from scratch: 60+ components across four products, cutting build time by roughly a third.
- Led design for the advisor-assist AI feature: search, summarisation, and decision support, including how confidence is signalled to a case worker before they act on it.
- Ran a two-year research programme with 90+ participants across three regional offices, feeding findings straight into roadmap decisions.
- Wrote a small agentic prototype in Python that validates data-entry claims against a source-of-truth ledger before a claim reaches a human reviewer.
- Redesigned the claims-intake workflow end to end, cutting average handling time by a fifth.
"""

_FAKE_LETTER = """Globex's bet is that claims handling should feel like a conversation, not a form, and that's a hard problem I've spent my career on from the other side of the table.

Most of my recent work has been building a component system for a fraud-detection platform from nothing: roughly sixty components across four products, which shaved about a third off how long a new screen took to ship.

I also led the design of an AI assistant that helps a case worker search prior claims and get a summarised recommendation, with the harder problem being how you show that worker how much to trust what the model just told them before they act on it.

On the side, I built a small Python prototype that checks a claim's data entry against the source ledger before it ever reaches a human reviewer, which taught me more about where automated checks quietly fail than any amount of reading about it would have.

What draws me to Globex specifically is how small the team stays close to the actual claim, and I'd like to talk about where that's heading next."""


def test_paraphrase_duplication_is_none_when_no_cv_text_given():
    assert sg.scan_paraphrase_duplication("Some letter text.", None) is None


def test_paraphrase_duplication_catches_a_reworded_bullet_with_no_shared_5grams():
    """The whole point of this metric: catch what scan_duplication (exact
    5-gram overlap) structurally cannot. This is what real restatement
    actually looks like — the same key nouns (design system, products,
    build time) survive a rewrite even when no five-word run does; a
    paraphrase that swaps out the nouns too, not just the sentence
    structure, is a harder case this bag-of-words method genuinely can't
    catch — see the module docstring's "what it still can't do" note."""
    cv = "- Built the company's first design system from scratch across seven products, cutting build time by half."
    letter = ("I built a design system from nothing, which now spans seven products and cut how "
              "long it takes the team to build a new screen by roughly half.")
    exact = sg.scan_duplication(letter, cv)
    assert exact["shared_ngrams"] == 0, "the fixture must genuinely share no 5-grams, or it isn't testing what it claims to"
    paraphrase = sg.scan_paraphrase_duplication(letter, cv)
    assert paraphrase["max_similarity"] >= sg.PARAPHRASE_SIMILARITY_THRESHOLD


def test_paraphrase_duplication_separates_restated_from_original_paragraphs():
    """Calibration fixture — see module comment above. Reproduces, on
    fictional content, the exact separation and threshold behavior found
    calibrating this metric against the real letter that motivated it."""
    r = sg.scan_paraphrase_duplication(_FAKE_LETTER, _FAKE_CV)
    assert r["paragraphs_scored"] == 5
    sims = [pp["max_similarity"] for pp in r["per_paragraph"]]
    opening, restated, closing = sims[0], sims[1:4], sims[4]
    assert opening < sg.PARAPHRASE_SIMILARITY_THRESHOLD
    assert closing < sg.PARAPHRASE_SIMILARITY_THRESHOLD
    assert all(s >= sg.PARAPHRASE_SIMILARITY_THRESHOLD for s in restated)
    assert r["paragraphs_above_threshold"] == 3


def test_paraphrase_duplication_skips_a_gap_only_paragraph():
    cv = "- Shipped a redesign of the intake workflow across three teams."
    letter = ("I'm drawn to the shape of this role and how close it stays to the work itself.\n\n"
              "[GAP: no evidence in the CV of the specific tool named in the posting]")
    r = sg.scan_paraphrase_duplication(letter, cv)
    assert r["paragraphs_scored"] == 1


# ------------------------------------------------------------------
# run_style_gate — top-level shape
# ------------------------------------------------------------------

def test_run_style_gate_returns_every_check():
    result = sg.run_style_gate("A short test letter with nothing much in it, not really.")
    assert set(result.keys()) == {
        "word_count", "em_dashes", "vocabulary", "editorializing",
        "conclusion_reflex", "false_ranges", "triplets",
        "negative_parallelism", "duplication", "duplication_paraphrase",
    }
    assert result["duplication"] is None  # no cv_text passed
    assert result["duplication_paraphrase"] is None


def test_run_style_gate_includes_duplication_when_cv_text_given():
    result = sg.run_style_gate("Some letter text.", cv_text="- Some CV bullet text.")
    assert result["duplication"] is not None
    assert result["duplication_paraphrase"] is not None


# ------------------------------------------------------------------
# Seam-risk proxies (implementation pass 3)
# ------------------------------------------------------------------

def test_paragraph_openings_returns_the_first_n_words_normalized():
    text = "What draws me here IS the shape of the team.\n\nI also led design for AI features."
    openings = sg.paragraph_openings(text, n_words=3)
    assert openings == ["what draws me", "i also led"]


def test_paragraph_openings_skips_a_gap_only_paragraph():
    text = "What draws me is this role.\n\n[GAP: no evidence of the named tool]"
    assert sg.paragraph_openings(text) == ["what draws me"]


def test_paragraph_openings_detects_intra_letter_repetition():
    """The mechanical check for writing-rules.md §4's "parallel-everything
    paragraphs" tell: every paragraph in this letter opens identically."""
    text = "I led the redesign of the checkout flow.\n\nI led the research programme end to end.\n\nI led the design system from scratch."
    openings = sg.paragraph_openings(text, n_words=2)
    assert openings == ["i led"] * 3
    assert len(set(openings)) == 1


def test_sentence_length_stats_computes_mean_and_stdev():
    text = "Short one. This one is a fair bit longer than the first. Medium length here now."
    stats = sg.sentence_length_stats(text)
    assert stats["n"] == 3
    assert stats["mean"] > 0
    assert stats["stdev"] > 0


def test_sentence_length_stats_is_zero_variance_for_uniform_sentences():
    """The mechanical shape of writing-rules.md §5's "uniform cadence is a
    tell" — every sentence here is exactly five words, so stdev must be 0."""
    text = "This is five words here. Also this one has five. One more five word one."
    stats = sg.sentence_length_stats(text)
    assert stats["stdev"] == 0.0


def test_sentence_length_stats_strips_gap_markers_before_splitting():
    text = "A short sentence. [GAP: this should not count as a sentence]"
    stats = sg.sentence_length_stats(text)
    assert stats["n"] == 1


def test_sentence_length_stats_handles_empty_text():
    assert sg.sentence_length_stats("") == {"mean": 0.0, "stdev": 0.0, "n": 0}


# ------------------------------------------------------------------
# Cross-letter phrase repetition (implementation pass 4, Task 4)
# ------------------------------------------------------------------

def test_most_people_applying_fixture_is_caught_across_letters():
    """The named regression fixture: this exact phrase was found, reading
    the actual generated text, recurring across four different companies'
    after-letters in a real experiment run."""
    letters = {
        27: "Most people applying here will make the craft case from Figma.",
        63: "Most people applying here will make the craft case, I think.",
        89: "Most people applying here will make the craft case from Figma. Mine is different.",
        3: "Something entirely unrelated about IT operations at Globex.",
    }
    results = sg.scan_cross_letter_phrases(letters)
    matched = [r for r in results if "most people applying" in r["phrase"]]
    assert matched
    assert set(matched[0]["letter_ids"]) == {27, 63, 89}
    assert 3 not in matched[0]["letter_ids"]


def test_no_shared_phrases_returns_empty():
    letters = {1: "I led the redesign of the checkout flow end to end.",
               2: "Globex's premise is that IT teams should trust automated signal."}
    assert sg.scan_cross_letter_phrases(letters) == []


def test_a_phrase_used_only_within_one_letter_is_not_reported():
    """Repeating a phrase inside a single letter is a different tell
    (writing-rules.md §4's parallel-everything paragraphs, covered by
    paragraph_openings) — this check is specifically about repetition
    ACROSS letters, so a phrase confined to one letter, however many times
    it appears there, must not be reported."""
    letters = {1: "I ship working software. I ship working software again and again.",
               2: "Something with no overlap at all in it whatsoever here."}
    assert sg.scan_cross_letter_phrases(letters) == []


def test_a_longer_shared_span_does_not_report_redundant_shorter_windows():
    """A shared run of exactly n_max words must not also surface as two
    separate, nearly-identical n_max-1 findings for the same letter set."""
    letters = {
        1: "the quick brown fox jumps over the lazy dog today",
        2: "the quick brown fox jumps over the lazy dog again",
    }
    results = sg.scan_cross_letter_phrases(letters, n_min=3, n_max=6)
    phrases = [r["phrase"] for r in results]
    assert "the quick brown fox jumps over the lazy dog" in phrases
    assert len([p for p in phrases if "quick brown fox" in p]) == 1


def test_shorter_phrase_kept_when_its_letter_set_differs_from_the_longer_ones():
    """The dedup only drops a shorter phrase when a longer one covers the
    exact same letters — a shorter phrase shared by MORE letters than any
    longer superset phrase is real information and must survive."""
    letters = {
        1: "at fnz i led the redesign of advisor tools",
        2: "at fnz i led the discovery programme end to end",
        3: "at fnz i ran a completely different initiative",
    }
    results = sg.scan_cross_letter_phrases(letters, n_min=3, n_max=6)
    three_letter_matches = [r for r in results if set(r["letter_ids"]) == {1, 2, 3}]
    assert any(r["phrase"] == "at fnz i" for r in three_letter_matches)


def test_scan_new_letter_against_corpus_finds_a_match():
    corpus = {10: "Most people applying here will make the craft case from Figma."}
    matches = sg.scan_new_letter_against_corpus(
        "Most people applying here will make the craft case, I think.", corpus)
    assert matches
    assert matches[0]["matches_stored_letters"] == [10]


def test_scan_new_letter_against_corpus_returns_empty_when_nothing_shared():
    corpus = {10: "Completely unrelated content about a different role entirely."}
    matches = sg.scan_new_letter_against_corpus(
        "I ship AI features end to end at a wealth platform.", corpus)
    assert matches == []
