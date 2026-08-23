#!/usr/bin/env python3
"""
Numeric fact gate (Phase 4 close).
===================================
Blocking, not advisory — the counterpart to verifier.py's entity check.
Numbers are discrete: unlike a rephrased claim, a number either matches a
source or it's invented, so the false-positive rate is low enough to gate on
without a human in the loop. Numbers are also where interview liability
actually lives — team sizes, assets under administration, locations, years —
which is why this is a hard block and the entity check isn't.

Every numeric token in a generated draft (percentages, currency, magnitude-
suffixed counts like 700+ or $2T, plain counts, years) is diffed against a
corpus built from three sources: the master CV used for that generation, the
job description, and professional-profile.md. Anything not found in the
corpus BLOCKS — the draft is never stored.

Comma-grouping and magnitude-suffix normalization ported, with attribution,
from career-ops' verify-cv-facts.mjs (normalizeClaim / COUNT_CLAIM_RE — MIT
License, santifer/career-ops, github.com/santifer/career-ops). See
CREDITS.md for the exact provenance. Everything else here is this project's
own and is a materially different design, not a port: career-ops only checks
a number when it sits next to one of a closed list of "metric nouns" and
layers a separate warn/block policy on top; this file checks every numeric
token unconditionally and blocks unconditionally, on the premise that
numbers are discrete enough not to need that extra qualification — a
rephrase can't hide an invented number the way it can hide a soft claim.
"""

import re

_CURRENCY = r"[$€£]"
_MAGNITUDE = r"[kKmMbBtT]"
_MAGNITUDE_WORDS = {"thousand": "k", "million": "m", "billion": "b", "trillion": "t"}
_MAGNITUDE_WORD_ALT = "|".join(_MAGNITUDE_WORDS)

# Every numeric token: optional currency prefix, optional leading '~'
# (approximation — the number is the claim, the hedge isn't, so it's
# stripped in normalization, not excluded here), digits with optional comma
# grouping and decimal, then EITHER an abbreviated magnitude suffix directly
# adjacent (must end the token, so "50kg" doesn't misread as "50k" — a
# negative lookahead for a following word character rather than \b, since
# \b alone doesn't distinguish "50kg" from "50k" right after the k) OR a
# spelled-out magnitude word with a space ("$2 trillion" — real surface
# variant found calibrating against a real generated draft, which wrote out
# what the master CV abbreviates as "$2T"; normalize_number folds the two to
# the same token), then optional '%' or trailing '+'.
_NUMERIC_TOKEN_RE = re.compile(
    rf"~?{_CURRENCY}?\s?\d[\d,]*(?:\.\d+)?"
    rf"(?:{_MAGNITUDE}(?!\w)|\s+(?:{_MAGNITUDE_WORD_ALT})\b)?%?\+?",
    re.IGNORECASE,
)


def normalize_number(token):
    """Strip the approximation prefix, fold a spelled-out magnitude word to
    its abbreviated letter, strip thousands-grouping commas (only when a
    comma is followed by exactly three digits, so a real list like "2, 3"
    or a stray comma elsewhere is untouched), and drop trailing punctuation
    the extraction regex can sweep in at a sentence boundary ("...in August
    2025," -> the comma is punctuation, not part of the year). So the same
    number compares equal however it's written: "16,181" / "16181" and
    "$2 trillion" / "$2T" both normalize the same way. Comma-grouping logic
    ported from career-ops' normalizeClaim() — see module docstring and
    CREDITS.md; the magnitude-word folding and trailing-punctuation strip
    are this project's own, found calibrating against real drafts."""
    t = token.strip().lstrip("~").lower()
    for word, letter in _MAGNITUDE_WORDS.items():
        t = re.sub(rf"\s+{word}\b", letter, t)
    t = re.sub(r"(\d),(?=\d{3}(?!\d))", r"\1", t)
    t = t.rstrip(",.")
    return t


def extract_numeric_tokens(text):
    """Every numeric token in text, normalized, deduplicated (a set — this
    gate checks presence, not count)."""
    return {normalize_number(m.group(0)) for m in _NUMERIC_TOKEN_RE.finditer(text or "")}


def build_corpus(*texts):
    """Concatenate the sources a numeric claim may legitimately be drawn
    from: master CV text, job description, professional-profile.md. Order
    doesn't matter — every source is checked as one bag of numbers."""
    return "\n".join(t or "" for t in texts)


def check_numeric_facts(draft_text, corpus_text):
    """Diff every numeric token in draft_text against corpus_text.
    Returns {"blocked": bool, "unmatched": [str, ...], "checked": int}."""
    corpus_numbers = extract_numeric_tokens(corpus_text)
    draft_numbers = extract_numeric_tokens(draft_text)
    unmatched = sorted(draft_numbers - corpus_numbers)
    return {
        "blocked": bool(unmatched),
        "unmatched": unmatched,
        "checked": len(draft_numbers),
    }
