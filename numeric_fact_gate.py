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

Every number in a generated draft (percentages, currency, magnitude-suffixed
counts like 700+ or $2T, plain counts, years, and number words) is diffed
against a corpus built from three sources: the master CV used for that
generation, the job description, and professional-profile.md. Anything not
found in the corpus fails the gate.

This module decides what is sourced. It does NOT decide what happens next.
Since 2026-09-29 an unmatched number starts a repair ladder rather than
ending the generation — see numeric_repair.py and the ladder in app.py.
Two rules of that ladder constrain this file:

  * What the gate accepts never loosens because a repair is available. The
    rules below (the "N+" understatement rule, the number-word mapping) are
    equivalences — two ways of writing the same number — not concessions.
  * A number that passes because of one of those rules says so.
    check_numeric_facts returns them under "equivalences" and the caller
    records them on the document, because a gate that quietly starts
    accepting more than it used to is indistinguishable from a broken one.

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


# ---------------------------------------------------------------------------
# Number words (rung 1 of the repair ladder)
# ---------------------------------------------------------------------------
# A draft that writes "from 0 to 3" against a CV that says "from zero to a
# team of 3" was blocking on 0, and a draft that writes "seven products" was
# never checked at all. Both are the same missing rule: a number word is a
# number. Mapping them on BOTH sides closes the false positive and the hole
# together.
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_UNIT_WORDS = [w for w, v in _NUMBER_WORDS.items() if 1 <= v <= 9]
_TENS_WORDS = [w for w, v in _NUMBER_WORDS.items() if v in (20, 30, 40, 50, 60, 70, 80, 90)]

# Longest alternatives first so "seventeen" is never matched as "seven".
_WORD_ALT = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_UNIT_ALT = "|".join(sorted(_UNIT_WORDS, key=len, reverse=True))
_TENS_ALT = "|".join(sorted(_TENS_WORDS, key=len, reverse=True))

# Compounds are matched whole. Without this, "twenty-five designers" would
# emit 20 and 5 — two numbers the corpus has never heard of — and block a
# draft that was correct, which is the exact failure this pass exists to
# stop. Only two-part compounds are handled ("N hundred", "tens-units");
# anything longer ("seven hundred and fifty") is left alone rather than
# half-read, which is what the "and" exclusion below is for.
_NUMBER_WORD_RE = re.compile(
    rf"(?<![\w-])(?:(?:{_UNIT_ALT})[-\s]hundred|(?:{_TENS_ALT})[-\s](?:{_UNIT_ALT})|(?:{_WORD_ALT}))(?![\w-])",
    re.IGNORECASE,
)

# "One" is the one number word that is usually not a quantity in English,
# and a false positive here blocks a draft that was fine. "one of the first
# UK banks" is a position, not a count.
_NON_QUANTIFYING_AFTER = {"of", "another", "and"}
_NON_QUANTIFYING_BEFORE = {"no", "any", "some", "every", "the"}

_WORD_RE = re.compile(r"[A-Za-z']+")


def _word_value(phrase):
    parts = re.split(r"[-\s]+", phrase.lower())
    if len(parts) == 2:
        left, right = (_NUMBER_WORDS[x] for x in parts)
        return left * right if right == 100 else left + right
    return _NUMBER_WORDS[parts[0]]


def _quantifies(text, start, end):
    """A number word counts only when it is quantifying something: a word
    follows it, that word isn't one that turns it into a phrase ("one OF
    the first"), and it isn't preceded by a determiner that does the same
    ("NO one", "THE one thing"). A number word at the end of a sentence is
    left alone — conservative on purpose, since the cost of a false
    positive here is a blocked draft that was correct."""
    before = _WORD_RE.findall(text[:start])
    after = _WORD_RE.findall(text[end:end + 40])
    if not after:
        return False
    if after[0].lower() in _NON_QUANTIFYING_AFTER:
        return False
    if before and before[-1].lower() in _NON_QUANTIFYING_BEFORE:
        return False
    return True


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


def extract_numeric_mentions(text):
    """Every number in text as {"surface", "value", "kind"} — surface is
    what was written, value is the normalized token, kind is "digits" or
    "words".

    Surfaces are kept because the gate has to be able to say WHY a number
    it once would have blocked is now acceptable: "'zero' and '0' are the
    same number" is a reportable equivalence, and a set of normalized
    tokens has already thrown away the evidence for it."""
    text = text or ""
    mentions = [{"surface": m.group(0).strip(), "value": normalize_number(m.group(0)),
                 "kind": "digits"}
                for m in _NUMERIC_TOKEN_RE.finditer(text)]
    for m in _NUMBER_WORD_RE.finditer(text):
        if not _quantifies(text, m.start(), m.end()):
            continue
        mentions.append({"surface": m.group(0), "value": str(_word_value(m.group(0))),
                         "kind": "words"})
    return mentions


def extract_numeric_tokens(text):
    """Every numeric token in text, normalized, deduplicated (a set — this
    gate checks presence, not count)."""
    return {m["value"] for m in extract_numeric_mentions(text)}


def build_corpus(*texts):
    """Concatenate the sources a numeric claim may legitimately be drawn
    from: master CV text, job description, professional-profile.md. Order
    doesn't matter — every source is checked as one bag of numbers."""
    return "\n".join(t or "" for t in texts)


def _is_sourced(number, corpus_numbers):
    """A draft number is sourced if it appears verbatim, OR if the draft
    drops the '+' from a source's "N+" ("35+ designers" in the JD written as
    "over 35 designers" / "35 designers" / "~35 designers" in the letter).
    Dropping the '+' can only understate the claim, so it's safe. The
    reverse stays blocked: a draft "N+" against a source that only says "N"
    inflates the claim. Found 2026-09-29 when two real cover-letter
    drafts blocked on "700" (CV: "700+ components") and "35" (JD: "35+
    product designers") — real, sourced numbers, reworded."""
    if number in corpus_numbers:
        return True
    return not number.endswith("+") and (number + "+") in corpus_numbers


def check_numeric_facts(draft_text, corpus_text):
    """Diff every number in draft_text against corpus_text.

    Returns {"blocked", "unmatched", "checked", "equivalences"}.
    "equivalences" names every number that is sourced only because one of
    the rules above says two surface forms are the same number — the "N+"
    rule or the number-word mapping. Nothing is fixed invisibly: a draft
    that got through because "zero" and "0" are the same number says so,
    and the caller records it in critic_notes."""
    corpus_mentions = extract_numeric_mentions(corpus_text)
    corpus_by_value = {}
    for m in corpus_mentions:
        corpus_by_value.setdefault(m["value"], set()).add(m["surface"].lower())
    corpus_numbers = set(corpus_by_value)

    unmatched, equivalences, seen = [], [], set()
    for mention in extract_numeric_mentions(draft_text):
        value, surface = mention["value"], mention["surface"]
        if value in seen:
            continue
        seen.add(value)
        if value in corpus_numbers:
            # Sourced. Worth reporting only when the two sides wrote it
            # differently — a word against digits, or the other way.
            corpus_kinds = {c["kind"] for c in corpus_mentions if c["value"] == value}
            if mention["kind"] not in corpus_kinds:
                equivalences.append({
                    "rule": "number-word", "draft": surface,
                    "source": sorted(corpus_by_value[value])[0],
                })
            continue
        if not value.endswith("+") and (value + "+") in corpus_numbers:
            equivalences.append({
                "rule": "n-plus", "draft": surface,
                "source": sorted(corpus_by_value[value + "+"])[0],
            })
            continue
        unmatched.append(value)

    return {
        "blocked": bool(unmatched),
        "unmatched": sorted(unmatched),
        "checked": len(seen),
        "equivalences": equivalences,
    }
