#!/usr/bin/env python3
"""
Source-fidelity verifier (Phase 3a).
=====================================
Deterministic, no second API call. Extracts company/title/product-like
phrases, years, and numbers/percentages from generated content and diffs each
against the master CV text used for that generation. Anything with no match
is a flag. Advisory only — it never blocks generation or approval; results
land in documents.critic_notes for the UI to surface next to the document.

Runs on content_md for both doc types (the CV's markdown projection included),
so one code path covers CVs and cover letters.
"""

import re

_GAP_RE = re.compile(r"\[GAP:[^\]]*\]")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")
# No apostrophe in the word class: "Nudelman's" matches as "Nudelman" (the
# possessive suffix breaks the run instead of merging into the next word),
# and contractions ("That's", "I've") reduce to their leading pronoun/word,
# which the stopword trim below then drops.
_PHRASE_RE = re.compile(r"\b[A-Z][a-zA-Z&]*(?:\s+[A-Z][a-zA-Z&]*){0,3}\b")

# Common capitalized function/connective words — sentence-initial capitals,
# section headers, contraction remnants — that aren't claims worth checking.
# Trimmed from the ends of a matched run rather than requiring the whole
# phrase to be stopwords, so "At FNZ I" reduces to "FNZ" instead of surviving
# whole or being dropped whole.
_STOPWORDS = {
    "the", "this", "that", "these", "those", "a", "an", "i", "we", "you", "he",
    "she", "it", "they", "in", "on", "at", "for", "with", "and", "or", "but",
    "as", "of", "to", "from", "by", "is", "are", "was", "were", "be", "been",
    "being", "has", "have", "had", "will", "would", "can", "could", "may",
    "might", "must", "should", "not", "no", "yes", "my", "his", "her", "its",
    "their", "our", "profile", "experience", "education", "languages",
    "what", "lead", "earlier", "gap", "none", "all", "most", "before",
    "after", "during", "across", "raising",
}


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%]+", " ", s.lower())).strip()


def _strip_headers_and_gaps(text):
    text = _GAP_RE.sub(" ", text)
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines)


def _extract_numeric(text):
    out = []
    for m in _YEAR_RE.finditer(text):
        out.append(("year", m.group(0)))
    for m in _NUMBER_RE.finditer(text):
        tok = m.group(0)
        if _YEAR_RE.fullmatch(tok):
            continue  # already captured as a year
        out.append(("number", tok))
    return out


def _extract_phrases(text):
    out = []
    for m in _PHRASE_RE.finditer(text):
        words = m.group(0).split()
        while words and words[0].lower() in _STOPWORDS:
            words.pop(0)
        while words and words[-1].lower() in _STOPWORDS:
            words.pop()
        if not words:
            continue
        phrase = " ".join(words)
        if len(phrase) < 3:
            continue
        out.append(("phrase", phrase))
    return out


def verify_fidelity(generated_md, master_cv_text):
    """Return {"flags": [{"kind","text"}], "gaps": [str]}. Deterministic."""
    gaps = _GAP_RE.findall(generated_md or "")
    body = _strip_headers_and_gaps(generated_md or "")
    master_norm = _norm(master_cv_text or "")

    flags = []
    seen = set()
    for kind, token in _extract_numeric(body) + _extract_phrases(body):
        norm = _norm(token)
        if not norm or (kind, norm) in seen:
            continue
        seen.add((kind, norm))
        if not re.search(r"\b" + re.escape(norm) + r"\b", master_norm):
            flags.append({"kind": kind, "text": token})

    return {"flags": flags, "gaps": gaps}
