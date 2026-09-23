#!/usr/bin/env python3
"""
Style gate (Phase: cover-letter quality, first pass).
========================================================
Deterministic, no LLM call, advisory only — same relationship to
numeric_fact_gate.py that verify_fidelity has: it reports, it never blocks.
numeric_fact_gate blocks because a number is discrete (a rephrase can't hide
an invented one). Prose style isn't discrete the same way — a negative-
parallelism regex will occasionally flag a genuinely earned contrast, an
em-dash count alone can't tell "sprinkled for punch" from "one deliberate
aside" — so this starts advisory and stays advisory until the fixture set in
docs/cover-letter-baseline.md shows a low false-positive rate. Promoting any
one check to a hard block, the way numeric_fact_gate already is, is a later
decision, not this one.

Findings doc: docs/cover-letter-quality-findings.md, diagnosis §1 and
intervention 4. The gap this closes: writing-rules.md's rules are loaded
verbatim into the generation system prompt (prompt_assembly.py), but nothing
downstream ever checks a draft against them — the two existing post-
generation checks (verifier.verify_fidelity, numeric_fact_gate) both target
invented candidate experience, a different failure mode than a banned
rhetorical pattern or an over-length letter. This module is that missing
check, built directly against the real writing-rules.md this project's
config.yaml points at (config['paths']['writing_rules'] — the sample at
sample-profile/writing-rules.md carries the same rules, since it's a
genuinely reusable standard, not a placeholder). Every list below is
transcribed from that source, not summarised.

Single implementation, two callers: the runtime advisory gate in app.py's
run_fidelity_gates (lands in critic_notes, same as verify_fidelity's flags)
and docs/cover-letter-baseline.md's offline scan over every stored letter.
Sharing one module means tuning a regex once fixes both — the two must never
drift into checking different things for the same rule.

Every check function returns {"count": int, "matches": [str, ...]} (or a
structure built from those), never just a boolean — a reviewer tuning a
threshold needs to see what actually matched, not just that something did.
"""

import re

# ------------------------------------------------------------------
# Negative parallelism — writing-rules.md §1: 'Negative parallelism —
# "It's not X, it's Y."... Kill it unless the contrast is doing real, earned
# work. Same goes for "Not only… but also."'
#
# Split into named sub-shapes, counted separately rather than folded into
# one total, because the copula-negation-then-restate shape ("wasn't X, it
# was Y") is the one a naive \bnot\b-only regex misses — it's a contraction,
# not the word "not" — and it was in fact the shape missed calibrating this
# gate against a real stored letter: "The hard problem there wasn't the
# AI, it was how you signal confidence..." is
# the purest instance of the whole pattern in that letter, and a first-pass
# regex here matching only literal "not" missed it entirely. Named as its
# own category rather than merged into "comma_not" so that miss can't repeat
# silently — it now has its own fixture in tests/test_style_gate.py.
# ------------------------------------------------------------------

_COMMA_NOT_JUST_RE = re.compile(r"\w,\s+not\s+just\s+[^,.;!?]{2,80}", re.IGNORECASE)
# Excludes "not just" so it doesn't double-count with the pattern above —
# each matched span belongs to exactly one category.
_COMMA_NOT_RE = re.compile(r"\w,\s+not\s+(?!just\b)[^,.;!?]{2,80}", re.IGNORECASE)

_NEG_COPULA = r"(?:wasn't|isn't|aren't|weren't|was\s+not|is\s+not|are\s+not|were\s+not)"
_RESTATE = r"(?:it|that|this|they|he|she)(?:'s|'re|\s+was|\s+is|\s+were|\s+are)\b"
_COPULA_NEGATION_RESTATE_RE = re.compile(
    rf"\b{_NEG_COPULA}\b[^,.;!?]{{0,80}},\s*{_RESTATE}[^,.;!?]{{0,80}}", re.IGNORECASE
)

# Sentence-initial "Not just X, Y" — distinct from the mid-sentence
# "X, not just Y" shape above (_COMMA_NOT_JUST_RE), same underlying tell in
# a different clause position.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")


def _split_sentences(text):
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


def _sentence_initial_not_just(text):
    matches = [s for s in _split_sentences(text) if re.match(r"^not just\b", s, re.IGNORECASE)]
    return {"count": len(matches), "matches": matches}


_NOT_ABOUT_RE = re.compile(
    r"\bit(?:'s|\s+is)\s+not\s+about\b[^,.;!?]{0,80},\s*it(?:'s|\s+is)\s+about\b[^,.;!?]{0,80}",
    re.IGNORECASE,
)
# Allows a comma in the gap ("not only X, but also Y" is the common form
# with the Oxford-style comma before "but also") — only . ; ! ? end the
# search window, not a comma.
_NOT_ONLY_BUT_ALSO_RE = re.compile(r"\bnot\s+only\b[^.;!?]{0,80}\bbut\s+also\b[^.;!?]{0,80}",
                                    re.IGNORECASE)

# Advisory-only within the advisory-only gate: these two connectives are
# often the honest, non-AI-tell way to draw a contrast ("hands-on craft
# rather than management"), so they're counted for visibility, never rolled
# into a violation total.
_RATHER_THAN_RE = re.compile(r"\brather than\b", re.IGNORECASE)
_INSTEAD_OF_RE = re.compile(r"\binstead of\b", re.IGNORECASE)


def _find(regex, text):
    matches = [m.group(0).strip() for m in regex.finditer(text or "")]
    return {"count": len(matches), "matches": matches}


def scan_negative_parallelism(text):
    """Every negative-parallelism sub-shape named in writing-rules.md §1,
    counted separately. `total_strict` sums the shapes that are always worth
    a second look; `rather_than_instead_of` is reported but excluded from
    that total since those two connectives are frequently legitimate."""
    result = {
        "comma_not": _find(_COMMA_NOT_RE, text),
        "comma_not_just": _find(_COMMA_NOT_JUST_RE, text),
        "copula_negation_restate": _find(_COPULA_NEGATION_RESTATE_RE, text),
        "not_just_sentence_initial": _sentence_initial_not_just(text),
        "not_about_pair": _find(_NOT_ABOUT_RE, text),
        "not_only_but_also": _find(_NOT_ONLY_BUT_ALSO_RE, text),
        "rather_than_instead_of": _find(re.compile(
            rf"(?:{_RATHER_THAN_RE.pattern})|(?:{_INSTEAD_OF_RE.pattern})", re.IGNORECASE
        ), text),
    }
    strict_keys = ("comma_not", "comma_not_just", "copula_negation_restate",
                   "not_just_sentence_initial", "not_about_pair", "not_only_but_also")
    result["total_strict"] = sum(result[k]["count"] for k in strict_keys)
    return result


# ------------------------------------------------------------------
# Vocabulary — writing-rules.md §2, transcribed verbatim from the cursed-word
# list and the stock-connective-phrase list. Word-boundary match for single
# words; phrase match (case-insensitive substring on a normalized string)
# for multi-word entries, since "/" variants (leverage/leveraging isn't
# listed but seamless/seamlessly and unlock/unleash are) need both forms
# checked.
# ------------------------------------------------------------------

CURSED_WORDS = [
    "delve", "intricate", "tapestry", "pivotal", "underscore", "underscores",
    "landscape", "foster", "testament", "enhance", "crucial", "realm",
    "navigate", "navigating", "robust", "seamless", "seamlessly", "leverage",
    "holistic", "nuanced", "multifaceted", "vibrant", "boast", "boasts",
    "garner", "showcase", "elevate", "resonate", "embark", "myriad",
    "plethora", "encompass", "dynamic", "comprehensive", "meticulous",
    "vital", "profound", "ever-evolving", "fast-paced", "game-changer",
    "paradigm shift", "deep dive", "unlock", "unleash", "harness",
    "spearhead", "cutting-edge", "state-of-the-art", "transformative",
    "groundbreaking",
]

STOCK_PHRASES = [
    "it's important to note that", "it's worth noting", "when it comes to",
    "in today's fast-paced world", "in today's digital world",
    "at the end of the day", "needless to say", "that being said",
    "in the realm of", "plays a key role in", "plays a vital role in",
]

# writing-rules.md §1 "Editorializing": AI inserts the reader's reaction for
# them. Distinct list from STOCK_PHRASES even though "It's important to
# note" appears in both source lists (§1 and §2) — that overlap is in the
# source document itself, not duplicated here by mistake.
EDITORIALIZING_PHRASES = [
    "it's important to note", "it's worth mentioning", "notably",
    "interestingly", "remarkably",
]

# writing-rules.md §1 "Conclusion reflex".
CONCLUSION_REFLEX_PHRASES = ["overall,", "in summary", "in conclusion", "ultimately,"]


def _word_matches(words, text):
    text_lower = (text or "").lower()
    out = []
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", text_lower):
            out.append(w)
    return out


def _phrase_matches(phrases, text):
    text_lower = (text or "").lower()
    return [p for p in phrases if p in text_lower]


def scan_vocabulary(text):
    cursed = _word_matches(CURSED_WORDS, text)
    stock = _phrase_matches(STOCK_PHRASES, text)
    return {
        "cursed_words": {"count": len(cursed), "matches": cursed},
        "stock_phrases": {"count": len(stock), "matches": stock},
    }


def scan_editorializing(text):
    matches = _phrase_matches(EDITORIALIZING_PHRASES, text)
    return {"count": len(matches), "matches": matches}


def scan_conclusion_reflex(text):
    matches = _phrase_matches(CONCLUSION_REFLEX_PHRASES, text)
    return {"count": len(matches), "matches": matches}


# ------------------------------------------------------------------
# False ranges — writing-rules.md §1: '"From intimate gatherings to global
# movements."... The "from X to Y" frame implies a spectrum that isn't
# there.' Purely informational: a literal date or location range ("from 2020
# to 2023") is legitimate and this regex can't tell that apart from a
# manufactured one, which the source document itself frames as a judgment
# call, not a mechanical one. Never counted toward any violation total.
# ------------------------------------------------------------------

_FALSE_RANGE_RE = re.compile(r"\bfrom\s+[^,.;!?]{3,60}?\s+to\s+[^,.;!?]{3,60}?\b", re.IGNORECASE)


def scan_false_ranges(text):
    return _find(_FALSE_RANGE_RE, text)


# ------------------------------------------------------------------
# Rule of threes — writing-rules.md §1: triplet lists. Counted per paragraph,
# not flagged, since "one triplet is fine" per the rule — it's a density
# check, not a per-instance violation.
# ------------------------------------------------------------------

# The comma before "and" is required, not optional. Without it, this
# regex was matching straight through ordinary two-item "X and Y"
# conjunctions that happen to follow an unrelated comma earlier in the
# sentence — e.g. "...workflows myself, working directly with product and
# engineering leadership..." read as a three-item list when it's really a
# clause boundary plus a plain "product and engineering" pair. Requiring
# the Oxford comma (list item, list item, and list item) is what actually
# distinguishes a triplet from prose that merely contains both a comma and
# an "and". Items capped at three words each — real triplets ("assisted
# search, summarisation, and decision support") are short; a longer run
# is a clause, not a list item.
_TRIPLET_RE = re.compile(
    r"\b[A-Za-z][\w'-]*(?:\s[\w'-]+){0,2},\s*[A-Za-z][\w'-]*(?:\s[\w'-]+){0,2},\s*and\s+"
    r"[A-Za-z][\w'-]*(?:\s[\w'-]+){0,2}\b"
)


def _split_paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def scan_triplets(text):
    paragraphs = _split_paragraphs(text)
    per_paragraph_matches = [[m.group(0).strip() for m in _TRIPLET_RE.finditer(p)] for p in paragraphs]
    return {
        "total": sum(len(m) for m in per_paragraph_matches),
        "by_paragraph": [len(m) for m in per_paragraph_matches],
        "matches": [m for para in per_paragraph_matches for m in para],
    }


# ------------------------------------------------------------------
# Punctuation — writing-rules.md §3: em dash overkill.
# ------------------------------------------------------------------

def scan_em_dashes(text):
    return {"count": (text or "").count("—")}


# ------------------------------------------------------------------
# Length. No word-count rule exists anywhere in the generation pipeline
# today ("one page" in COVER_LETTER_TASK is prose, not enforced) — this is
# the first mechanical check against it. 300 is a starting ceiling, not a
# measured one; tune against docs/cover-letter-baseline.md.
# ------------------------------------------------------------------

DEFAULT_WORD_CEILING = 300


def scan_word_count(text, ceiling=DEFAULT_WORD_CEILING):
    count = len((text or "").split())
    return {"count": count, "ceiling": ceiling, "over_ceiling": count > ceiling}


# ------------------------------------------------------------------
# CV-duplication proxy (findings doc §2/§3: intervention 2's backstop).
# Shared 5-gram overlap between the letter and the tailored CV's own
# content_md, expressed as a percentage of the letter's unique 5-grams.
# A strict proxy — it catches near-verbatim restatement, not paraphrase-
# level duplication (the same fact reworded in different words shares few or
# no 5-grams even when a human reader would call it duplication). Report
# this limitation plainly wherever the number is shown; don't let a low
# score be read as "no duplication."
# ------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _words(text):
    return _WORD_RE.findall((text or "").lower())


def _ngrams(words, n):
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)} if len(words) >= n else set()


def scan_duplication(letter_text, cv_text, n=5):
    if not cv_text:
        return None
    letter_ngrams = _ngrams(_words(letter_text), n)
    cv_ngrams = _ngrams(_words(cv_text), n)
    if not letter_ngrams:
        return {"shared_ngrams": 0, "letter_ngrams": 0, "overlap_pct": 0.0, "examples": []}
    shared = letter_ngrams & cv_ngrams
    pct = round(100 * len(shared) / len(letter_ngrams), 1)
    examples = [" ".join(g) for g in sorted(shared)[:8]]
    return {
        "shared_ngrams": len(shared), "letter_ngrams": len(letter_ngrams),
        "overlap_pct": pct, "examples": examples,
    }


# ------------------------------------------------------------------
# Paraphrase-aware duplication (second implementation pass). scan_duplication
# above is a floor: near-verbatim restatement only. It cannot see the actual
# failure mode this project cares about — a CV bullet reworded into letter
# prose shares almost no exact 5-word runs with its source. This is the
# ceiling: per-letter-paragraph TF-IDF cosine similarity against every
# individual line of the tailored CV, reporting the best match each
# paragraph found rather than one aggregate number, because "this paragraph
# restates that bullet" is the actionable unit — a percentage alone doesn't
# tell you which paragraph to cut.
#
# Method: hand-rolled TF-IDF (no sklearn dependency, per instruction) —
# term frequency per unit, inverse document frequency computed once across
# every unit in this one role's letter+CV (paragraphs and CV lines both
# count as "documents" for IDF purposes, so a word common across the whole
# corpus, "design", "I", "product", is downweighted the way TF-IDF is
# supposed to downweight it), cosine similarity between each letter
# paragraph's vector and every CV line's vector, keeping the best match per
# paragraph. A CV "line" is any markdown bullet ("- ...") in content_md —
# bullets are the natural addressable unit a Task 3 plan's cv_source_line
# pointer would name, and they're where the facts actually live; headers,
# dates, and company/role lines are excluded as metadata, not claims.
#
# Calibrated against real ground truth, not picked blind: a real stored
# cover letter and its own tailored CV (both real, neither committable —
# see tests/test_style_gate.py's fictional fixture below for the
# committed, reproducible version of this same check). A human reader (the
# original bug report this whole project traces back to) called four of
# the letter's six paragraphs CV-restatement — two AI/product paragraphs
# and two design-system paragraphs — and the opening and closing
# paragraphs original, not restated. This metric's scores on that letter:
# opening 0.20, closing 0.17, and the four restated paragraphs 0.34-0.52 —
# a clean gap, and thresholding at 0.3 reproduces "four of six" exactly,
# independently of the human read that first named it. That's the bar this
# metric had to clear before being trusted; see
# tests/test_style_gate.py::test_paraphrase_duplication_separates_restated_from_original_paragraphs
# for the fixture that locks the calibration in. What it still can't do:
# tell a genuinely coincidental topical overlap (two paragraphs both
# mentioning "design systems" in an unrelated sense) from real restatement
# — it's a bag-of-words method, blind to argument structure, so a high
# score is a strong "go read this paragraph" signal, not a verdict on its
# own.
# ------------------------------------------------------------------

import math
from collections import Counter, defaultdict

PARAPHRASE_SIMILARITY_THRESHOLD = 0.3

_CV_BULLET_RE = re.compile(r"^-\s+(.*)$")


_PROFILE_HEADING_RE = re.compile(r"^##\s*profile\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{2,}\s")


def _profile_paragraph(cv_text):
    """The prose under a "## Profile" heading (up to the next ## heading),
    if the CV has one. Added in pass 4 after a real generation cited a
    tenure/summary fact ("17 years building information-dense, expert-user
    products...") that lives only in this paragraph, not in any bullet —
    "years of experience" and similar framing claims are profile-paragraph
    material by nature, not per-role achievement material, so a
    cv_source_line check that only recognizes bullets rejected a genuine,
    correctly-sourced claim. Returns "" if there's no such section."""
    lines = (cv_text or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if _PROFILE_HEADING_RE.match(ln.strip())), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if _HEADING_RE.match(lines[i].strip())),
               len(lines))
    return " ".join(lines[start + 1:end]).strip()


def extract_cv_lines(cv_text):
    """Every citable unit in the tailored CV: each markdown bullet ("- ..."),
    plus (pass 4) each sentence of the "## Profile" paragraph if one exists
    — a bullet is naturally the per-role achievement unit, but a tenure or
    positioning claim ("17 years building...") lives in the profile
    paragraph instead, and a cv_source_line check that only recognized
    bullets rejected exactly that kind of genuine claim in real use."""
    lines = []
    for raw in (cv_text or "").splitlines():
        m = _CV_BULLET_RE.match(raw.strip())
        if m and m.group(1).strip():
            lines.append(m.group(1).strip())
    profile = _profile_paragraph(cv_text)
    if profile:
        lines.extend(s for s in _split_sentences(profile) if s.strip())
    return lines


_GAP_RE = re.compile(r"\[GAP:[^\]]*\]")


def _non_gap_paragraphs(text):
    """Paragraphs with GAP markers stripped, dropping any paragraph that was
    nothing but a GAP marker — that's meta-commentary about a missing fact,
    not a claim to score against the CV."""
    out = []
    for p in _split_paragraphs(text):
        stripped = _GAP_RE.sub(" ", p).strip()
        if stripped:
            out.append(p.strip())
    return out


def _idf(token_lists):
    n = len(token_lists)
    df = Counter()
    for tokens in token_lists:
        for t in set(tokens):
            df[t] += 1
    # Smoothed idf (the standard sklearn-style formula): every term gets a
    # positive weight even at df == n, so a word in every single unit still
    # contributes a small amount rather than zeroing out the whole vector.
    return {t: math.log((1 + n) / (1 + df[t])) + 1 for t in df}


def _tf(tokens):
    counts = Counter(tokens)
    total = sum(counts.values())
    return {t: c / total for t, c in counts.items()} if total else {}


def _tfidf_vector(tokens, idf):
    tf = _tf(tokens)
    return {t: tf[t] * idf.get(t, 0.0) for t in tf}


def _cosine(v1, v2):
    common = set(v1) & set(v2)
    numerator = sum(v1[t] * v2[t] for t in common)
    mag1 = math.sqrt(sum(x * x for x in v1.values()))
    mag2 = math.sqrt(sum(x * x for x in v2.values()))
    return numerator / (mag1 * mag2) if mag1 and mag2 else 0.0


def scan_paraphrase_duplication(letter_text, cv_text, threshold=PARAPHRASE_SIMILARITY_THRESHOLD):
    if not cv_text:
        return None
    paragraphs = _non_gap_paragraphs(letter_text)
    cv_lines = extract_cv_lines(cv_text)
    if not paragraphs or not cv_lines:
        return {"per_paragraph": [], "max_similarity": 0.0, "median_similarity": 0.0,
                "paragraphs_above_threshold": 0, "paragraphs_scored": len(paragraphs),
                "threshold": threshold}

    corpus_tokens = [_words(p) for p in paragraphs] + [_words(l) for l in cv_lines]
    idf = _idf(corpus_tokens)
    cv_vectors = [(_tfidf_vector(_words(l), idf), l) for l in cv_lines]

    per_paragraph = []
    for i, p in enumerate(paragraphs):
        v = _tfidf_vector(_words(p), idf)
        best_sim, best_line = 0.0, ""
        for cv_vec, cv_line in cv_vectors:
            sim = _cosine(v, cv_vec)
            if sim > best_sim:
                best_sim, best_line = sim, cv_line
        per_paragraph.append({
            "paragraph_index": i, "excerpt": p[:80], "max_similarity": round(best_sim, 3),
            "best_match": best_line,
        })

    sims = sorted(pp["max_similarity"] for pp in per_paragraph)
    n = len(sims)
    median = sims[n // 2] if n % 2 else round((sims[n // 2 - 1] + sims[n // 2]) / 2, 3)
    return {
        "per_paragraph": per_paragraph,
        "max_similarity": max(sims) if sims else 0.0,
        "median_similarity": median,
        "paragraphs_above_threshold": sum(1 for s in sims if s >= threshold),
        "paragraphs_scored": len(paragraphs),
        "threshold": threshold,
    }


# ------------------------------------------------------------------
# Seam-risk proxies (implementation pass 3): two deterministic checks for
# whether a letter, or a set of letters, reads "assembled" rather than
# written — the risk raised against the plan-then-write design before it
# had ever been run for real. Neither is part of run_style_gate's per-
# letter advisory output; both are corpus-level tools the cover-letter-
# experiment script calls directly, since "does this letter's rhythm look
# templated" is inherently a comparison across letters, not one letter's
# own score.
# ------------------------------------------------------------------

def paragraph_openings(text, n_words=3):
    """The first n_words of every non-GAP paragraph, normalized (lowercase,
    whitespace-collapsed). writing-rules.md §4 names "parallel-everything
    paragraphs... too symmetrical to be human" as a structural tell; this is
    the mechanical proxy for it — intra-letter repetition (the same letter's
    paragraphs all opening the same way) and, once compared across many
    letters' outputs, cross-letter repetition (the same opening formula
    reused for different companies) — the stronger signal that a template
    is showing through, not just one letter."""
    return [" ".join(_words(p)[:n_words]) for p in _non_gap_paragraphs(text)]


def sentence_length_stats(text):
    """Mean and population standard deviation of sentence length (in words)
    across the whole letter. writing-rules.md §5: "Vary rhythm. Mix short
    punchy sentences with longer ones. Uniform cadence is a tell." A falling
    stdev between two versions of the same kind of document is the
    mechanical shape of that tell — prose flattening toward one rhythm, not
    a judgment call the way "does this read assembled" otherwise would be."""
    stripped = _GAP_RE.sub(" ", text or "")
    lengths = [n for n in (len(_words(s)) for s in _split_sentences(stripped)) if n > 0]
    if not lengths:
        return {"mean": 0.0, "stdev": 0.0, "n": 0}
    mean = sum(lengths) / len(lengths)
    variance = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return {"mean": round(mean, 1), "stdev": round(variance ** 0.5, 2), "n": len(lengths)}


# ------------------------------------------------------------------
# Cross-letter phrase repetition (implementation pass 4, Task 4). The
# corpus-level check nothing before this could do: every gate up to this
# one scores a single document in isolation, so a phrase like "most people
# applying" showing up in four different companies' letters, found reading
# the pass-3 experiment's saved text, was invisible to all of them. This is
# the one check that gets more useful the longer the tool runs, since the
# corpus it compares against only grows.
#
# This check has NO opinion about whether a repeated phrase is bad, only
# that it repeats. Some repetition is a person having a consistent voice
# ("I ship working software" showing up twice because it's genuinely how
# this candidate talks); some is a template showing through. Only a human
# reading the actual letters can tell those apart, the same reason
# false_ranges is informational-only elsewhere in this module. "Most people
# applying" (found across four after-letters in the pass-3 experiment) is
# the fixture below precisely because it's the confirmed real example, not
# a hypothetical.
# ------------------------------------------------------------------

def _ngram_phrases(text, n_min, n_max):
    """Every n-word phrase (n_min to n_max, inclusive) in text, as a set of
    (n, phrase_string) — deduplicated within this one text, so a letter
    using the same phrase three times internally still counts as one use
    when checked against other letters."""
    words = _words(_GAP_RE.sub(" ", text or ""))
    out = set()
    for n in range(n_min, n_max + 1):
        for i in range(len(words) - n + 1):
            out.add((n, " ".join(words[i:i + n])))
    return out


def scan_cross_letter_phrases(letters, n_min=3, n_max=6):
    """letters: {letter_id: text}. Finds every 3-to-6-word phrase used in 2
    or more different letters, deduplicated so a shorter phrase isn't
    reported separately when a longer phrase containing it already covers
    the exact same set of letters (no information lost by dropping it) —
    kept when the longer version's letter set is a proper subset, since
    that means the shorter phrase shows real repetition the longer one
    doesn't capture. Returns a list of {"phrase", "n", "letter_ids"}
    sorted by how many letters share it, then by phrase length, both
    descending (the most-shared, most-specific findings first)."""
    per_letter = {lid: _ngram_phrases(text, n_min, n_max) for lid, text in letters.items()}
    phrase_letters = defaultdict(set)
    for lid, phrases in per_letter.items():
        for key in phrases:
            phrase_letters[key].add(lid)
    shared = {key: ids for key, ids in phrase_letters.items() if len(ids) >= 2}

    keep = []
    for (n, phrase), ids in shared.items():
        redundant = False
        for (n2, phrase2), ids2 in shared.items():
            if n2 > n and phrase in phrase2 and ids2 == ids:
                redundant = True
                break
        if not redundant:
            keep.append({"phrase": phrase, "n": n, "letter_ids": sorted(ids, key=str)})

    # A shared span longer than n_max words survives the dedup above as a
    # family of overlapping n_max-word windows (all tied at n == n_max, no
    # single one strictly containing the others), which would otherwise be
    # reported as several near-duplicate findings for one real repetition.
    # Chain-merge windows that share the same letter set and overlap by
    # n_max-1 words into the single longer phrase they actually are.
    by_ids = defaultdict(list)
    for r in keep:
        if r["n"] == n_max:
            by_ids[tuple(r["letter_ids"])].append(r["phrase"])
    merged_away = set()
    extra = []
    for ids, phrases in by_ids.items():
        remaining = set(phrases)
        while remaining:
            chain = remaining.pop()
            merged_away.add(chain)
            grew = True
            while grew:
                grew = False
                for p in list(remaining):
                    p_words, chain_words = p.split(), chain.split()
                    if chain_words[-(n_max - 1):] == p_words[:n_max - 1]:
                        chain = chain + " " + p_words[-1]
                        remaining.discard(p)
                        merged_away.add(p)
                        grew = True
                    elif p_words[-(n_max - 1):] == chain_words[:n_max - 1]:
                        chain = p_words[0] + " " + chain
                        remaining.discard(p)
                        merged_away.add(p)
                        grew = True
            extra.append({"phrase": chain, "n": len(chain.split()), "letter_ids": list(ids)})

    keep = [r for r in keep if not (r["n"] == n_max and r["phrase"] in merged_away)] + extra
    keep.sort(key=lambda r: (len(r["letter_ids"]), r["n"]), reverse=True)
    return keep


def scan_new_letter_against_corpus(new_text, corpus, n_min=3, n_max=6):
    """Runtime advisory use (Task 4): does this one new letter share a
    3-to-6-word phrase with anything already stored for this profile?
    corpus: {letter_id: text} of previously stored letters (the caller
    supplies these — e.g. every other cover_letter document in cockpit.db
    for this candidate). Returns the same shape as scan_cross_letter_phrases,
    filtered to matches that involve the new letter, with the new letter's
    key normalized to "new" in the output rather than whatever the caller's
    internal id scheme is, since a runtime caller cares "does this repeat
    something," not which specific prior document it repeats."""
    letters = {**corpus, "__new__": new_text}
    results = scan_cross_letter_phrases(letters, n_min, n_max)
    out = []
    for r in results:
        if "__new__" in r["letter_ids"]:
            other_ids = [i for i in r["letter_ids"] if i != "__new__"]
            out.append({"phrase": r["phrase"], "n": r["n"],
                        "matches_stored_letters": other_ids})
    return out


# ------------------------------------------------------------------
# Top-level entry point — the single shape both app.py (runtime, advisory,
# lands in critic_notes) and the baseline scan import and call.
# ------------------------------------------------------------------

def run_style_gate(content_md, cv_text=None, word_ceiling=DEFAULT_WORD_CEILING):
    return {
        "word_count": scan_word_count(content_md, word_ceiling),
        "em_dashes": scan_em_dashes(content_md),
        "vocabulary": scan_vocabulary(content_md),
        "editorializing": scan_editorializing(content_md),
        "conclusion_reflex": scan_conclusion_reflex(content_md),
        "false_ranges": scan_false_ranges(content_md),
        "triplets": scan_triplets(content_md),
        "negative_parallelism": scan_negative_parallelism(content_md),
        "duplication": scan_duplication(content_md, cv_text),
        "duplication_paraphrase": scan_paraphrase_duplication(content_md, cv_text),
    }


# ------------------------------------------------------------------
# Row/table helpers — one implementation shared by the baseline scan below
# and docs/cover-letter-experiment-1.md's before/after script (Task 4 of
# implementation pass 2), so the two reports are never built from two
# slightly-different column definitions.
# ------------------------------------------------------------------

BASELINE_COLUMNS = [
    ("id", "id"), ("company", "company"), ("title", "title"), ("category", "category"),
    ("words", "words"), ("over_ceiling", "over 300"), ("em_dashes", "em-"),
    ("cursed_words", "cursed"), ("stock_phrases", "stock"), ("editorializing", "editor."),
    ("conclusion_reflex", "concl."), ("triplets", "triplets"),
    ("neg_parallelism_strict", "neg-parallel"), ("rather_than", "rather-than"),
    ("dup_pct", "dup% (5-gram)"), ("dup_paraphrase_max", "dup-max (paraphrase)"),
    ("dup_paraphrase_above", "dup-paras-above/scored"),
]


def style_gate_row(row_id, meta, content_md, cv_text):
    """meta: a dict of identifying/extra columns (company, title, category,
    and — for the Task 4 experiment — anything else worth carrying, like
    company_evidence_empty). Returns one row dict, same shape regardless of
    caller, for build_baseline_table below."""
    result = run_style_gate(content_md, cv_text=cv_text)
    neg = result["negative_parallelism"]
    dup5 = result["duplication"]
    dupp = result["duplication_paraphrase"]
    row = {
        "id": row_id, "words": result["word_count"]["count"],
        "over_ceiling": result["word_count"]["over_ceiling"],
        "em_dashes": result["em_dashes"]["count"],
        "cursed_words": result["vocabulary"]["cursed_words"]["count"],
        "stock_phrases": result["vocabulary"]["stock_phrases"]["count"],
        "editorializing": result["editorializing"]["count"],
        "conclusion_reflex": result["conclusion_reflex"]["count"],
        "triplets": result["triplets"]["total"],
        "neg_parallelism_strict": neg["total_strict"],
        "rather_than": neg["rather_than_instead_of"]["count"],
        "dup_pct": dup5["overlap_pct"] if dup5 else None,
        "dup_paraphrase_max": dupp["max_similarity"] if dupp else None,
        "dup_paraphrase_above": (f"{dupp['paragraphs_above_threshold']}/{dupp['paragraphs_scored']}"
                                  if dupp else None),
    }
    row.update(meta)
    return row


def format_baseline_table(rows, columns=BASELINE_COLUMNS):
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, sep]
    for r in rows:
        cells = [str(r.get(key, "")) if r.get(key) is not None else "n/a" for key, _ in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _latest_cv_text(conn, role_id):
    row = conn.execute(
        "SELECT content_md FROM documents WHERE role_id = ? AND doc_type = 'cv' "
        "ORDER BY version DESC LIMIT 1",
        (role_id,),
    ).fetchone()
    return row["content_md"] if row else None


if __name__ == "__main__":
    # Offline baseline scan (findings doc §3): every stored cover letter in
    # cockpit.db, scored with no model call. Prints a markdown table to
    # stdout — `python3 style_gate.py > docs/cover-letter-baseline.md`
    # regenerates the numbers on record any time a prompt change lands.
    # Real cockpit.db, deliberately: this is a manual, read-only offline
    # tool run directly by a person, not part of the pytest suite (which
    # never touches the real database — see tests/conftest.py).
    import db as dbmod

    conn = dbmod.connect()
    letters = conn.execute(
        "SELECT d.id, d.role_id, r.company, r.title, r.category, d.version, d.content_md "
        "FROM documents d JOIN roles r ON r.id = d.role_id "
        "WHERE d.doc_type = 'cover_letter' ORDER BY d.id"
    ).fetchall()

    rows = []
    for letter in letters:
        cv_text = _latest_cv_text(conn, letter["role_id"])
        meta = {"company": letter["company"], "title": letter["title"], "category": letter["category"]}
        rows.append(style_gate_row(letter["id"], meta, letter["content_md"], cv_text))
    conn.close()

    print(f"# Cover letter baseline ({len(rows)} letters, {dbmod.today()})\n")
    print(format_baseline_table(rows))

    n = len(rows)
    if n:
        print(f"\n<!-- summary: n={n}, "
              f"median_words={sorted(r['words'] for r in rows)[n // 2]}, "
              f"over_ceiling={sum(1 for r in rows if r['over_ceiling'])}, "
              f"any_neg_parallelism={sum(1 for r in rows if r['neg_parallelism_strict'] > 0)}, "
              f"any_em_dash={sum(1 for r in rows if r['em_dashes'] > 0)} -->")
