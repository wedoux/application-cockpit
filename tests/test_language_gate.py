"""
Language gate (Swiss-specific). Deterministic extraction across the four
languages Swiss postings are written in, a 3-outcome classifier that never
silently blocks, and the decision-memory close-the-loop on Decline.
"""
import re

import pytest

import db as dbmod
import language_gate as lg
import prompt_assembly as pa

DECLARED = {"English": "C2", "Greek": "native", "French": "B2", "Spanish": "B1"}


# ------------------------------------------------------------------
# config.yaml <-> master CV agreement (item 1)
# ------------------------------------------------------------------

@pytest.mark.parametrize("category", ["Leadership", "Advisory", "Senior IC"])
def test_declared_languages_appear_in_each_master_cv(category):
    cfg = pa.load_config()
    declared = cfg["profile"]["languages"]
    _, master_cv_text = pa.resolve_master_cv(category, cfg)
    # Master CVs write languages under a "## Languages" heading — either a
    # single line ("English (C2) · French (B2)") or a bulleted list
    # ("- English — native\n- French — B2"), both real formats in use here.
    # Capture the WHOLE section body, not just its first line — a
    # first-line-only capture silently passed for the single-line format
    # and silently under-checked the multi-line one.
    m = re.search(r"##\s*Languages\s*\n+(.*?)(?=\n##|\Z)", master_cv_text,
                   re.IGNORECASE | re.DOTALL)
    assert m, f"no Languages section found in the {category} master CV"
    languages_section = m.group(1).lower()
    for language in declared:
        assert language.lower() in languages_section, (
            f"{language} is declared in config.yaml but missing from the "
            f"{category} master CV's Languages section"
        )


# ------------------------------------------------------------------
# Detection across all four posting-writing languages (item 2, item 5 of
# the test list: "each of the four languages detected in each of the four
# posting languages")
# ------------------------------------------------------------------

# (target_language, posting_language, phrase) — each phrase states target_language
# as a requirement at C1, written in posting_language.
REQUIREMENT_PHRASES = [
    ("English", "English", "English required at C1 level."),
    ("English", "French", "Anglais courant requis (C1)."),
    ("English", "German", "Englischkenntnisse C1 erforderlich."),
    ("English", "Italian", "Inglese richiesto (livello C1)."),
    ("French", "English", "French required at C1 level."),
    ("French", "French", "Français requis (niveau C1)."),
    ("French", "German", "Französischkenntnisse C1 erforderlich."),
    ("French", "Italian", "Francese richiesto (livello C1)."),
    ("German", "English", "German required at C1 level."),
    ("German", "French", "Allemand requis (niveau C1)."),
    ("German", "German", "Deutschkenntnisse C1 erforderlich."),
    ("German", "Italian", "Tedesco richiesto (livello C1)."),
    ("Italian", "English", "Italian required at C1 level."),
    ("Italian", "French", "Italien requis (niveau C1)."),
    ("Italian", "German", "Italienischkenntnisse C1 erforderlich."),
    ("Italian", "Italian", "Italiano richiesto (livello C1)."),
]


@pytest.mark.parametrize("target_language,posting_language,phrase", REQUIREMENT_PHRASES,
                         ids=[f"{t}-in-{p}" for t, p, _ in REQUIREMENT_PHRASES])
def test_requirement_detected_in_every_posting_language(target_language, posting_language, phrase):
    reqs = lg.extract_language_requirements(phrase)
    matches = [r for r in reqs if r["language"] == target_language and r["kind"] == "required"]
    assert matches, f"failed to detect {target_language} as required in a {posting_language}-language posting: {phrase!r}"
    assert matches[0]["level"] == "C1"


def test_german_compound_word_is_detected():
    # "-kenntnisse" glued to the language stem with no space is the standard
    # German phrasing (Deutschkenntnisse) — verified this was a real gap
    # (zero matches) before LANGUAGE_NAMES included the compound forms.
    reqs = lg.extract_language_requirements("Gute Deutschkenntnisse sind Voraussetzung.")
    assert any(r["language"] == "German" and r["kind"] == "required" for r in reqs)


# ------------------------------------------------------------------
# Nice-to-have must never gate (item 2's "an asset" case, item 3)
# ------------------------------------------------------------------

def test_an_asset_phrasing_does_not_gate():
    # A real-world shape worth its own regression: two languages sharing
    # one trailing qualifier, joined by "and/or" rather than a comma.
    jd = "French and/or German an asset. Strong communication skills required."
    result = lg.classify_language_gate(jd, DECLARED)
    assert result["block"] == []
    assert result["warn"] == []
    kinds = {e["language"]: e["kind"] for e in lg.extract_language_requirements(jd)}
    assert kinds["French"] == "nice_to_have"
    assert kinds["German"] == "nice_to_have"


@pytest.mark.parametrize("phrase", [
    "German is a plus.",
    "German is an asset.",
    "L'allemand serait un plus.",
    "Deutschkenntnisse von Vorteil.",
    "La conoscenza del tedesco è gradita.",
])
def test_various_nice_to_have_phrasings_do_not_gate(phrase):
    result = lg.classify_language_gate(phrase, DECLARED)
    assert result["block"] == [], f"{phrase!r} should not block"


def test_bare_mention_with_no_signal_is_ambiguous_not_blocking():
    # A language named with no level, no requirement marker, no nice-to-have
    # marker — must not be guessed into either bucket.
    jd = "Team members come from Germany, France, and Italy."
    reqs = lg.extract_language_requirements(jd)
    # Whatever gets picked up here must be ambiguous, never required.
    assert all(r["kind"] != "required" for r in reqs if r["source"] == "phrase")


# ------------------------------------------------------------------
# The three outcomes (item 3)
# ------------------------------------------------------------------

def test_missing_language_blocks():
    jd = "Deutschkenntnisse (C1) erforderlich für diese Rolle."
    result = lg.classify_language_gate(jd, DECLARED)
    assert len(result["block"]) == 1
    assert result["block"][0]["language"] == "German"
    message = lg.format_block_message(result["block"])
    assert "German" in message and "C1" in message and "none declared" in message


def test_below_level_warns_but_does_not_block():
    # The exact WARN shape this gate exists for: French C1 required,
    # only B2 declared.
    jd = "Français requis (niveau C1) pour ce poste."
    result = lg.classify_language_gate(jd, DECLARED)
    assert result["block"] == []
    assert len(result["warn"]) == 1
    assert result["warn"][0]["language"] == "French"
    assert result["warn"][0]["level"] == "C1"


def test_declared_language_meeting_the_bar_neither_warns_nor_blocks():
    jd = "English required at C2 level."  # DECLARED above has English: C2
    result = lg.classify_language_gate(jd, DECLARED)
    assert result["block"] == []
    assert result["warn"] == []


def test_required_but_no_stated_level_is_a_note_not_a_warn():
    # Declared language, explicit requirement marker, but no level to
    # compare against — nothing to warn about, just record it.
    jd = "Spanish proficiency required."
    result = lg.classify_language_gate(jd, DECLARED)
    assert result["block"] == []
    assert result["warn"] == []
    assert any(e["language"] == "Spanish" for e in result["note"])


GERMAN_JD = """
Wir suchen eine erfahrene Führungskraft für unser Designteam in Zürich und Umgebung.
Sie übernehmen die Verantwortung für die Gestaltung unserer digitalen Produkte und
arbeiten eng mit den Teams aus Produkt, Technik und Compliance zusammen. Sie bringen
mehrjährige Erfahrung in der Führung von Designteams mit und haben ein gutes
Verständnis für regulierte Branchen wie Banking oder Versicherungen. Sie kommunizieren
sicher mit Stakeholdern auf allen Ebenen und können komplexe Sachverhalte verständlich
vermitteln. Wir bieten Ihnen ein modernes Arbeitsumfeld mit viel Gestaltungsspielraum
und die Möglichkeit, die Zukunft unseres Unternehmens aktiv mitzugestalten.
"""


def test_jd_own_language_is_a_fallback_signal_when_undeclared():
    # A posting entirely in German with no explicit "German required"
    # sentence — the JD's own language is itself the signal. A single short
    # sentence isn't a realistic stand-in for a real posting (hundreds to
    # thousands of words, per the real JDs in cockpit.db) and correctly
    # doesn't clear the confidence floor on its own — this uses a
    # paragraph-length excerpt instead.
    result = lg.classify_language_gate(GERMAN_JD, DECLARED)
    assert any(e["language"] == "German" and e["source"] == "jd_language" for e in result["block"])


def test_jd_own_language_does_not_duplicate_an_explicit_phrase_match():
    jd = "Deutschkenntnisse (C1) erforderlich. " + GERMAN_JD
    result = lg.classify_language_gate(jd, DECLARED)
    german_entries = [e for e in result["block"] + result["warn"] + result["note"] if e["language"] == "German"]
    assert len(german_entries) == 1, "the explicit phrase match should win, not stack with the jd_language fallback"
    assert german_entries[0]["source"] == "phrase"


# ------------------------------------------------------------------
# Decision memory close-the-loop (item 4)
# ------------------------------------------------------------------

def _insert_role(company, title, status="sourced", jd_text=None):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}", "manual", company, title, "Leadership", status, jd_text,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def test_decline_role_sets_status_and_decision_memory():
    jd = "Deutschkenntnisse (C1) erforderlich für diese Rolle."
    role_id = _insert_role("Swiss Bank AG", "Head of Design", jd_text=jd)

    result = lg.classify_language_gate(jd, DECLARED)
    conn = dbmod.connect()
    lg.decline_role(conn, role_id, result["block"])
    conn.close()

    conn = dbmod.connect()
    row = conn.execute("SELECT status, decision_reason, decision_note FROM roles WHERE id = ?",
                        (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "ignored"
    assert row["decision_reason"] == "language"
    assert "Deutschkenntnisse" in row["decision_note"], "the note must quote the requirement verbatim"


def test_declined_role_is_then_recognized_as_a_repost_by_the_scanner(monkeypatch):
    """The repeat-repost problem, solved by construction: once declined, a
    repost of the same role must link back via repost_of instead of
    surfacing as a fresh, undecided find — reusing the scanner's
    existing repost-linking (Phase: decision memory + repost lineage),
    not new logic."""
    import app as app_module

    jd = "Deutschkenntnisse (C1) erforderlich für diese Rolle."
    role_id = _insert_role("Swiss Bank AG", "Head of Design", jd_text=jd)
    result = lg.classify_language_gate(jd, DECLARED)
    conn = dbmod.connect()
    lg.decline_role(conn, role_id, result["block"])
    conn.close()

    match = {"job_id": "new-req-swiss-bank", "company": "Swiss Bank AG",
              "title": "Head of Design", "location": "Zurich",
              "url": "https://x", "portal": "test", "posted": "2026-08-20"}
    monkeypatch.setattr(app_module, "scan_all_companies_detailed", lambda: [{
        "company": "Swiss Bank AG", "portal": "test", "outcome": "ok", "error_detail": None,
        "raw_count": 1, "title_matches": [match], "dropped_titles": [],
        "location_matches": [match], "dropped_locations": [],
    }])
    app_module.scan_into_db()

    conn = dbmod.connect()
    row = conn.execute("SELECT repost_of FROM roles WHERE job_id = 'new-req-swiss-bank'").fetchone()
    conn.close()
    assert row["repost_of"] == role_id
