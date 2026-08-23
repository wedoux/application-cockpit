#!/usr/bin/env python3
"""
Language gate (Swiss-specific, Phase: first of its kind).
============================================================
Deterministic, no LLM call. Swiss postings are written in English, French,
German, or Italian, and routinely state language requirements in whichever
of those the posting itself is written in — so detection has to work
across all four, not just English.

Three outcomes, never a silent block:
  - a REQUIRED language not declared at all (config.yaml's
    profile.languages) -> BLOCK. The caller offers Decline or Override;
    Decline is closed via decline_role(), which records WHY in decision
    memory so the same role never comes back as a fresh surprise.
  - a REQUIRED language that IS declared, but the posting's bar is above
    the declared level -> WARN. Never blocks — the caller carries it
    forward the way an unverifiable claim already becomes a [GAP] marker.
  - a nice-to-have ("an asset", "un plus", "von Vorteil" ...) or a mention
    with no way to tell which it is -> NOTE only. Ambiguous is deliberately
    NOT treated as required: guessing wrong in the blocking direction is
    worse than surfacing it for a human to read.

Enforcing in /api/roles/<id>/generate (app.py) — see HANDOFF.md for the
promotion history (16/16 shadow-mode agreement, including 3 real BLOCKs
on genuine German requirements before it went live). This module is the
detector and classifier plus decline_role(); the BLOCK/Decline/Override
wiring itself lives in app.py.
"""

import re
from collections import Counter

import db as dbmod

# CEFR plus the two non-CEFR buckets this project needs: "native" (above C2)
# sits at the top: a native speaker always clears any CEFR bar.
LEVEL_ORDER = ["A1", "A2", "B1", "B2", "C1", "C2", "native"]
LEVEL_RANK = {lvl: i + 1 for i, lvl in enumerate(LEVEL_ORDER)}

# Fluency words that aren't themselves a CEFR code, mapped to the rank they
# most defensibly imply. "native" family -> above C2. "fluent" family
# (fluent/courant/fliessend/bilingue/verhandlungssicher) -> treated as a C1
# bar: this is a judgment call, not a standard, but C1 ("effective
# operational proficiency") is the closest CEFR analogue to how Swiss
# postings use these words, and it's the conservative (harder-to-clear)
# reading, which matters more for WARN precision than the exact label.
FLUENCY_WORD_RANK = {
    "native": LEVEL_RANK["native"],
    "maternelle": LEVEL_RANK["native"],
    "maternel": LEVEL_RANK["native"],
    "muttersprachlich": LEVEL_RANK["native"],
    "madrelingua": LEVEL_RANK["native"],
    "native or bilingual proficiency": LEVEL_RANK["native"],
    "fluent": LEVEL_RANK["C1"],
    "courant": LEVEL_RANK["C1"],
    "courante": LEVEL_RANK["C1"],
    "fliessend": LEVEL_RANK["C1"],
    "bilingue": LEVEL_RANK["C1"],
    "verhandlungssicher": LEVEL_RANK["C1"],
    # LinkedIn-style proficiency labels — the exact wording a master CV's
    # own languages line might use ("English, full professional, C2"),
    # and common in Swiss/int'l postings that borrow the same scale.
    "full professional proficiency": LEVEL_RANK["C2"],
    "professional working proficiency": LEVEL_RANK["B2"],
    "limited working proficiency": LEVEL_RANK["B1"],
    "elementary proficiency": LEVEL_RANK["A2"],
}

_CEFR_RE = re.compile(r"\b([ABC][12])\b", re.IGNORECASE)

# Canonical target language -> surface forms across the 4 posting-writing
# languages (English, French, German, Italian). This is what lets a French-
# or German-authored posting's own requirement sentence be read at all.
#
# "-kenntnisse" ("knowledge/skills") compounds are the standard German way
# to phrase a language requirement (Deutschkenntnisse, Englischkenntnisse)
# — glued to the language stem with no space, so plain \b-bounded matching
# on "deutsch"/"englisch" alone misses them entirely (verified: zero
# matches on "Englischkenntnisse C1 erforderlich" before this was added).
# Listed explicitly rather than matched with a prefix regex, because a
# prefix match on a bare stem risks "Deutschland" (the country) being read
# as a language requirement.
LANGUAGE_NAMES = {
    "English": ["english", "anglais", "englisch", "inglese", "englischkenntnisse"],
    "French": ["french", "français", "francais", "französisch", "franzosisch", "francese",
               "französischkenntnisse", "franzosischkenntnisse"],
    "German": ["german", "allemand", "deutsch", "tedesco", "deutschkenntnisse"],
    "Italian": ["italian", "italien", "italienisch", "italiano", "italienischkenntnisse"],
    "Spanish": ["spanish", "espagnol", "spanisch", "spagnolo", "spanischkenntnisse"],
}

NICE_TO_HAVE_MARKERS = [
    "a plus", "an asset", "nice to have", "desirable", "advantageous",
    "preferred but not required", "bonus", "beneficial", "would be a plus",
    "un atout", "un plus", "souhaité", "souhaitée", "apprécié", "appréciée", "serait un plus",
    "von vorteil", "wünschenswert", "ein plus",
    "un vantaggio", "gradito", "gradita",
]

REQUIREMENT_MARKERS = [
    "required", "must", "essential", "mandatory", "requirement", "requires", "necessary", "proficiency",
    "requis", "exigé", "exigée", "indispensable", "obligatoire",
    "erforderlich", "zwingend", "voraussetzung", "pflicht",
    "richiesto", "richiesta", "necessario", "obbligatorio",
]

# Word-frequency language detection for "the JD's own language is a signal"
# — cheap, deterministic, no LLM. Just enough stopwords per language to be
# a reliable majority vote on real posting text.
_STOPWORDS = {
    "English": {"the", "and", "of", "to", "for", "with", "is", "are", "in", "on", "you", "our"},
    "French": {"le", "la", "les", "de", "des", "et", "une", "un", "dans", "pour", "vous", "avec"},
    "German": {"der", "die", "das", "und", "mit", "für", "ein", "eine", "den", "zu", "sie", "ist"},
    "Italian": {"il", "la", "di", "che", "per", "con", "del", "una", "le", "gli", "sono", "questo"},
}


def _word_in(text_lower, phrase):
    return re.search(rf"\b{re.escape(phrase)}\b", text_lower) is not None


def _split_clauses(text):
    """Split on clause boundaries (newline, comma, semicolon, sentence-end)
    but NOT on "and"/"or" — a trailing qualifier ("an asset", "required")
    usually scopes a whole conjoined phrase ("French and/or German an
    asset"), so splitting there would separate a language from the very
    marker that classifies it."""
    parts = re.split(r"[\n;,]+|(?<=[a-zà-ÿ])\.\s+(?=[A-ZÀ-Ö])", text or "")
    return [p.strip() for p in parts if p.strip()]


def _extract_level(clause_lower):
    """The MINIMUM level mentioned in the clause, if any — "native or C1"
    means C1 clears the bar, so the lower of multiple mentioned levels is
    the actual threshold, not the higher one."""
    ranks = [LEVEL_RANK[m.group(1).upper()] for m in _CEFR_RE.finditer(clause_lower)]
    for word, rank in FLUENCY_WORD_RANK.items():
        if _word_in(clause_lower, word):
            ranks.append(rank)
    if not ranks:
        return None
    min_rank = min(ranks)
    for label, rank in LEVEL_RANK.items():
        if rank == min_rank:
            return label
    return None


# Confidence floor for detect_jd_language: a real full-length posting in
# one language racks up dozens of stopword hits; a short or incidental
# aside doesn't. Found by testing, not guessed — a one-sentence Italian
# aside ("La conoscenza del tedesco è gradita.") scored a "confident"
# Italian win on raw plurality alone (2 hits, nothing else in contention),
# which would have wrongly fed a BLOCK-strength jd_language signal off a
# single throwaway sentence. Both the absolute floor and the margin over
# the runner-up matter: the floor rejects short text outright, the margin
# rejects a text that's genuinely multi-lingual with no clear majority.
MIN_CONFIDENT_STOPWORDS = 8
MIN_CONFIDENT_MARGIN = 2.0


def detect_jd_language(jd_text):
    """Cheap stopword-majority vote across English/French/German/Italian.
    Returns None if the text is too short or the result isn't a clear
    majority — see MIN_CONFIDENT_STOPWORDS/MARGIN above."""
    words = re.findall(r"[a-zà-ÿ]+", (jd_text or "").lower())
    if not words:
        return None
    counts = Counter(words)
    scores = {lang: sum(counts.get(w, 0) for w in stop) for lang, stop in _STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_lang, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0
    if best_score < MIN_CONFIDENT_STOPWORDS:
        return None
    if runner_up_score > 0 and (best_score / runner_up_score) < MIN_CONFIDENT_MARGIN:
        return None
    return best_lang


def extract_language_requirements(jd_text):
    """Every language mention in jd_text, classified per clause. Returns a
    list of {"language", "level", "kind", "source", "excerpt"} dicts.
    kind is "required" | "nice_to_have" | "ambiguous". source is "phrase"
    (an explicit sentence) or "jd_language" (the posting's own language,
    used only as a fallback when no explicit phrase already covers it)."""
    found = []
    seen = set()
    for clause in _split_clauses(jd_text):
        clause_lower = clause.lower()
        langs_here = [lang for lang, forms in LANGUAGE_NAMES.items()
                      if any(_word_in(clause_lower, f) for f in forms)]
        if not langs_here:
            continue
        level = _extract_level(clause_lower)
        is_nice = any(_word_in(clause_lower, m) for m in NICE_TO_HAVE_MARKERS)
        is_required_marker = any(_word_in(clause_lower, m) for m in REQUIREMENT_MARKERS)
        if is_nice:
            kind = "nice_to_have"
        elif is_required_marker or level:
            kind = "required"
        else:
            kind = "ambiguous"
        for lang in langs_here:
            key = (lang, clause)
            if key in seen:
                continue
            seen.add(key)
            found.append({"language": lang, "level": level, "kind": kind,
                          "source": "phrase", "excerpt": clause[:200]})

    dominant = detect_jd_language(jd_text)
    if dominant and dominant != "English" and not any(f["language"] == dominant for f in found):
        found.append({"language": dominant, "level": None, "kind": "required",
                      "source": "jd_language", "excerpt": f"(posting is written in {dominant})"})
    return found


def classify_language_gate(jd_text, declared_languages):
    """declared_languages: {"English": "C2", "French": "B2", ...} — from
    config.yaml's profile.languages. Returns {"block": [...], "warn": [...],
    "note": [...]}, each a list of the requirement dicts from
    extract_language_requirements()."""
    block, warn, note = [], [], []
    for req in extract_language_requirements(jd_text):
        if req["kind"] in ("nice_to_have", "ambiguous"):
            note.append(req)
            continue
        lang, level = req["language"], req["level"]
        if lang not in declared_languages:
            block.append(req)
            continue
        if level is None:
            note.append(req)  # required and declared, but no bar stated to compare against
            continue
        declared_rank = LEVEL_RANK.get(declared_languages[lang])
        required_rank = LEVEL_RANK.get(level)
        if declared_rank is not None and required_rank is not None and declared_rank < required_rank:
            warn.append(req)
        else:
            note.append(req)
    return {"block": block, "warn": warn, "note": note}


def format_block_message(block_entries):
    parts = []
    for e in block_entries:
        lvl = f" {e['level']}" if e["level"] else ""
        parts.append(f"requires {e['language']}{lvl}, you have none declared")
    return "; ".join(parts)


def decline_role(conn, role_id, block_entries):
    """Close the loop (item 4): a Decline at the gate writes status='ignored'
    with decision_reason='language' and a note quoting the requirement
    verbatim, so the scanner's repost-linking (Phase: decision memory) keeps
    this role from ever surfacing as a fresh, undecided find again — a
    company that reposts the same German-required role repeatedly gets
    solved by construction here, not by remembering you already declined
    it last time. A single targeted row update, not a bulk mutation, so no
    backup_db call here — same precedent as api_update's other single-row
    decision writes."""
    quotes = "; ".join(f"\"{e['excerpt']}\"" for e in block_entries)
    note = f"Declined at the language gate: {quotes}"
    ts = dbmod.now()
    conn.execute(
        "UPDATE roles SET status = 'ignored', decision_reason = 'language', "
        "decision_note = ?, updated_at = ? WHERE id = ?",
        (note, ts, role_id),
    )
    conn.commit()
