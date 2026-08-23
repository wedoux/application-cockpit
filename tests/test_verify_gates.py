"""
Integration tests for the two hard gates wired into the live routes (Phase 4
close): the numeric fact gate in /api/roles/<id>/generate, and the ATS
text-layer gate in /api/documents/<id>/download. Also covers JD provenance
(jd_sha256 in critic_notes), which rides along with the numeric gate's edit
to the same code path.

Real API calls and real PDF rendering are both mocked here — this file tests
the WIRING (does app.py call the gate, act on its verdict, and leave the
database in the right state), not the gates' own logic (see
test_numeric_fact_gate.py and test_ats_verify.py for that).
"""

import hashlib
import json

import app
import ats_verify
import db as dbmod
import generation
import pdf_export


def _insert_role(company="Acme Corp", title="Head of Design", category="Leadership",
                  jd_text="A job description mentioning 7 designers and a $500k budget."):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "jd_source, first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}", "manual", company, title, category, "sourced",
         jd_text, "paste", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _fake_generate(content_md, master_cv_text="Led a team of 7 designers on a $500k budget."):
    def _gen(role, doc_type, config=None):
        return {
            "content_md": content_md,
            "content_json": None,
            "master_cv_text": master_cv_text,
            "master_cv_path": "/fake/master-cv.md",
            "model": "test-model",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read": 0, "cache_write": 0},
            "cost_usd": 0.0,
            "stop_reason": "end_turn",
            "truncated": False,
            "retried": False,
            "repairs": [],
        }
    return _gen


# ------------------------------------------------------------------
# Numeric fact gate — /api/roles/<id>/generate
# ------------------------------------------------------------------

def test_generate_blocks_on_an_unsourced_number(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(
        generation, "generate",
        _fake_generate("I led a team of 27 designers on a $500k budget."),  # 27 is invented
    )
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    assert r.status_code == 422
    data = r.get_json()
    assert data["ok"] is False
    assert data["error"] == "numeric_fact_gate"
    assert "27" in data["unmatched"]

    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM documents WHERE role_id = ?", (role_id,)).fetchone()[0]
    conn.close()
    assert n == 0, "a blocked draft must never be stored"


def test_generate_passes_when_every_number_is_sourced(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(
        generation, "generate",
        _fake_generate("I led a team of 7 designers on a $500k budget."),
    )
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    conn = dbmod.connect()
    row = conn.execute("SELECT critic_notes FROM documents WHERE role_id = ?", (role_id,)).fetchone()
    conn.close()
    notes = json.loads(row["critic_notes"])
    assert notes["numeric_gate"]["blocked"] is False


# ------------------------------------------------------------------
# JD provenance
# ------------------------------------------------------------------

def test_jd_sha256_is_recorded_in_critic_notes(monkeypatch):
    jd_text = "A job description mentioning 7 designers and a $500k budget."
    role_id = _insert_role(jd_text=jd_text)
    monkeypatch.setattr(
        generation, "generate",
        _fake_generate("I led a team of 7 designers on a $500k budget."),
    )
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    assert r.status_code == 200

    conn = dbmod.connect()
    row = conn.execute("SELECT critic_notes FROM documents WHERE role_id = ?", (role_id,)).fetchone()
    conn.close()
    notes = json.loads(row["critic_notes"])
    assert notes["jd_sha256"] == hashlib.sha256(jd_text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# ATS text-layer gate — /api/documents/<id>/download
# ------------------------------------------------------------------

def _insert_cv_document(role_id):
    conn = dbmod.connect()
    ts = dbmod.now()
    content_json = json.dumps({
        "profile": "Profile text.",
        "what_i_lead": [{"label": "A", "detail": "B"}],
        "experience": [{"role": "Head of Design", "company": "Acme Corp", "logo_key": None,
                        "dates": "2020", "context": "C", "bullets": ["Did a thing"]}],
        "education": [{"qualification": "BA", "institution": "Uni", "dates": "2007"}],
        "languages": [{"language": "English", "level": "C2"}],
    })
    doc_id = conn.execute(
        "INSERT INTO documents (role_id, doc_type, version, format, content_md, content_json, "
        "model, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (role_id, "cv", 1, "md", "placeholder", content_json, "test-model", "draft", ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return doc_id


def _stub_pdf_render(monkeypatch):
    """PDF rendering itself isn't what these tests check — stub it to a
    trivial file write so the gate-wiring tests don't need a real browser."""
    def _fake_html_to_pdf(html, path):
        from pathlib import Path
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 fake")
        return path
    monkeypatch.setattr(pdf_export, "html_to_pdf", _fake_html_to_pdf)


def test_download_blocks_on_an_ats_problem(monkeypatch):
    role_id = _insert_role()
    doc_id = _insert_cv_document(role_id)
    _stub_pdf_render(monkeypatch)
    monkeypatch.setattr(
        ats_verify, "verify_ats_text_layer",
        lambda *a, **k: {
            "cid_or_replacement_chars": ["1 (cid:N) marker"],
            "contact_literal_text": [], "reading_order": [], "internal_repetition": [],
            "posting_keywords": None, "page_count": 1, "extracted_chars": 10,
        },
    )
    client = app.app.test_client()
    r = client.get(f"/api/documents/{doc_id}/download")
    assert r.status_code == 422
    assert r.get_json()["error"] == "ats_gate_failed"

    conn = dbmod.connect()
    row = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    assert row["file_path"] is None, "a blocked PDF must never be cached as the served file"


def test_download_passes_a_clean_render(monkeypatch):
    role_id = _insert_role()
    doc_id = _insert_cv_document(role_id)
    _stub_pdf_render(monkeypatch)
    monkeypatch.setattr(
        ats_verify, "verify_ats_text_layer",
        lambda *a, **k: {
            "cid_or_replacement_chars": [], "contact_literal_text": [],
            "reading_order": [], "internal_repetition": [],
            "posting_keywords": None, "page_count": 1, "extracted_chars": 500,
        },
    )
    client = app.app.test_client()
    r = client.get(f"/api/documents/{doc_id}/download")
    assert r.status_code == 200

    conn = dbmod.connect()
    row = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    assert row["file_path"] is not None


def test_download_does_not_block_on_stuffed_keywords_alone(monkeypatch):
    """Regression: this exact shape (only posting_keywords.stuffed non-empty,
    everything else clean) blocked two real downloads live before the ratio
    check was taken out of the hard gate — see app.py's comment."""
    role_id = _insert_role()
    doc_id = _insert_cv_document(role_id)
    _stub_pdf_render(monkeypatch)
    monkeypatch.setattr(
        ats_verify, "verify_ats_text_layer",
        lambda *a, **k: {
            "cid_or_replacement_chars": [], "contact_literal_text": [],
            "reading_order": [], "internal_repetition": [],
            "posting_keywords": {"present": [], "honestly_absent": [],
                                  "stuffed": [("design", 25, 3)]},
            "page_count": 1, "extracted_chars": 500,
        },
    )
    client = app.app.test_client()
    r = client.get(f"/api/documents/{doc_id}/download")
    assert r.status_code == 200


# ------------------------------------------------------------------
# Language gate — enforcing. BLOCK stops /api/roles/<id>/generate before any
# generation call; the caller either declines the role or overrides.
# ------------------------------------------------------------------

def test_language_gate_block_stops_generation_before_any_api_call(monkeypatch):
    """A JD that BLOCKs under the language gate (German required, undeclared)
    must not generate — no document written, and generation.generate() must
    never even be called."""
    role_id = _insert_role(jd_text="Deutschkenntnisse (C1) erforderlich für diese Rolle.")
    calls = []
    monkeypatch.setattr(generation, "generate",
                         lambda *a, **kw: calls.append(1) or _fake_generate("x")(*a, **kw))

    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    d = r.get_json()
    assert r.status_code == 422
    assert d["ok"] is False
    assert d["error"] == "language_gate_block"
    assert len(d["block"]) == 1
    assert d["block"][0]["language"] == "German"
    assert calls == []

    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) c FROM documents WHERE role_id = ?", (role_id,)).fetchone()["c"]
    conn.close()
    assert n == 0


def test_language_gate_override_proceeds_and_records_it(monkeypatch):
    role_id = _insert_role(jd_text="Deutschkenntnisse (C1) erforderlich für diese Rolle.")
    monkeypatch.setattr(generation, "generate", _fake_generate("A clean draft with nothing to flag."))

    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate",
                     json={"doc_types": ["cover_letter"], "override_language_gate": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["documents"][0]["language_gate_verdict"] == "block"
    assert d["documents"][0]["language_gate_overridden"] is True

    conn = dbmod.connect()
    row = conn.execute("SELECT critic_notes FROM documents WHERE role_id = ?", (role_id,)).fetchone()
    conn.close()
    lg = json.loads(row["critic_notes"])["language_gate"]
    assert lg["verdict"] == "block"
    assert lg["overridden"] is True
    assert "shadow_mode" not in lg


def test_language_gate_decline_closes_the_loop():
    """The /language_gate/decline route wiring: recomputes the verdict
    server-side and, on a real block, writes status='ignored',
    decision_reason='language', decision_note quoting the requirement.
    (decline_role()'s own repost-linking behavior is covered directly in
    test_language_gate.py — this test is about the route, not the gate.)"""
    role_id = _insert_role(company="DEPT", title="Head of UX",
                            jd_text="Full professional proficiency in both German and English "
                                     "(C1 level) is mandatory.")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/language_gate/decline")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["status"] == "ignored"
    assert d["decision_reason"] == "language"
    assert "German" in d["decision_note"]

    conn = dbmod.connect()
    row = conn.execute("SELECT status, decision_reason, decision_note FROM roles WHERE id = ?",
                        (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "ignored"
    assert row["decision_reason"] == "language"
    assert "German" in row["decision_note"]


def test_language_gate_decline_refuses_when_nothing_blocks():
    role_id = _insert_role(jd_text="A job description mentioning 7 designers and a $500k budget.")
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/language_gate/decline")
    assert r.status_code == 400
    assert r.get_json()["error"] == "no_block"


def test_language_gate_clear_verdict_for_an_undemanding_jd(monkeypatch):
    role_id = _insert_role(jd_text="A job description mentioning 7 designers and a $500k budget.")
    monkeypatch.setattr(generation, "generate", _fake_generate("A clean draft with nothing to flag."))

    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    assert r.status_code == 200
    assert r.get_json()["documents"][0]["language_gate_verdict"] == "clear"


def test_language_gate_warn_never_blocks_generation(monkeypatch):
    """WARN (a declared language below the posting's required bar) must not
    block — same as before promotion to enforcing, only BLOCK does."""
    role_id = _insert_role(jd_text="- Français natif ou C1 requis pour ce poste.")
    monkeypatch.setattr(generation, "generate", _fake_generate("A clean draft with nothing to flag."))

    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cover_letter"]})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["documents"][0]["language_gate_verdict"] == "warn"


def test_language_gate_verdict_surfaces_in_the_documents_list(monkeypatch):
    role_id = _insert_role(jd_text="Deutschkenntnisse (C1) erforderlich für diese Rolle.")
    monkeypatch.setattr(generation, "generate", _fake_generate("A clean draft with nothing to flag."))
    client = app.app.test_client()
    client.post(f"/api/roles/{role_id}/generate",
                json={"doc_types": ["cover_letter"], "override_language_gate": True})

    r = client.get(f"/api/roles/{role_id}/documents")
    docs = r.get_json()["documents"]
    assert docs[0]["language_gate_verdict"] == "block"
    assert docs[0]["language_gate_overridden"] is True
