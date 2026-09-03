"""
Hand-editing a generated cover letter.
=======================================
BUILD-SPEC section 1 says "draft off -> edit, nothing is a dead end", and
until this landed the only fix for one wrong sentence was regenerating the
whole document.

The property that carries the feature is that the gates re-run against the
edited text through the SAME function generation uses. A gate you can get
around by typing the text yourself is not a gate, and a second copy of the
gate sequence in the save path would drift until whether a number is
acceptable depended on which door the text came through — the failure mode
staging.py avoids by calling into gmail_sweep_apply.py rather than
reimplementing duplicate detection.

Everything here builds documents by INSERT rather than by generating, so
nothing in this module makes an API call.
"""
import json
import re

import pytest

import app
import db as dbmod
import numeric_fact_gate as ng
import prompt_assembly as pa

_WRITE_SQL_RE = re.compile(r"^\s*(insert|update|delete|replace)\b", re.IGNORECASE)

MASTER_CV = "Led a team of 12 designers across 3 countries. Shipped in 2019.\n"
JD_TEXT = "Head of Design. You will lead a team of 12 and own the roadmap.\n"
PROFILE = "Design leader, 17 years in fintech.\n"

# Every number in here traces back to the corpus above.
CLEAN_LETTER = "I led 12 designers across 3 countries and shipped in 2019.\n"


class _WriteGuardedConn:
    """Raises the moment any write-shaped SQL runs through it. Same
    wrap-don't-subclass approach as test_gmail_sweep_apply.py's guard —
    sqlite3.Connection is a builtin type and can't be class-patched."""
    def __init__(self, real_conn):
        object.__setattr__(self, "_real", real_conn)

    def execute(self, sql, *args, **kwargs):
        if _WRITE_SQL_RE.match(sql):
            raise AssertionError(f"write statement executed on a blocked edit: {sql!r}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """A config pointing at throwaway CV and profile files, so these tests
    never read the real ones and can change the master CV mid-test."""
    cv_dir = tmp_path / "cv"
    cv_dir.mkdir()
    master = cv_dir / "cv-leadership.md"
    master.write_text(MASTER_CV)
    profile = tmp_path / "profile.md"
    profile.write_text(PROFILE)

    cfg = {
        "paths": {"cv_dir": str(cv_dir), "about_me": str(profile)},
        "category_cv_map": {"Leadership": "cv-leadership.md", "Review": None},
    }
    monkeypatch.setattr(pa, "load_config", lambda *a, **kw: cfg)
    return {"master": master, "profile": profile, "cfg": cfg}


@pytest.fixture
def client():
    return app.app.test_client()


def _role(jd_text=JD_TEXT, category="Leadership"):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("edit-test", "manual", "Acme Corp", "Head of Design", category, "drafted", jd_text,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _document(role_id, content_md=CLEAN_LETTER, doc_type="cover_letter", version=1,
              status="draft", model="claude-test", critic_notes=None, content_json=None):
    conn = dbmod.connect()
    doc_id = conn.execute(
        """INSERT INTO documents (role_id, doc_type, version, format, content_md,
            content_json, model, critic_notes, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (role_id, doc_type, version, "md", content_md, content_json, model,
         critic_notes, status, dbmod.now()),
    ).lastrowid
    conn.commit()
    conn.close()
    return doc_id


def _docs(role_id):
    conn = dbmod.connect()
    rows = conn.execute(
        "SELECT * FROM documents WHERE role_id = ? ORDER BY version", (role_id,)
    ).fetchall()
    conn.close()
    return rows


def _count_docs():
    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    return n


# ------------------------------------------------------------------
# The gate re-runs against the edited text
# ------------------------------------------------------------------

def test_an_edit_introducing_an_unmatched_number_is_rejected(client, sources):
    role_id = _role()
    doc_id = _document(role_id)
    before = _count_docs()

    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 47 designers across 3 countries.\n"})

    assert r.status_code == 422
    d = r.get_json()
    assert d["error"] == "numeric_fact_gate"
    assert "47" in d["unmatched"]
    assert _count_docs() == before, "a blocked edit must store nothing"


def test_a_blocked_edit_never_issues_a_write_statement(client, sources, monkeypatch):
    """A row count proves nothing was created; it doesn't prove nothing was
    attempted. This intercepts every statement the route's connection runs
    and fails on the attempt, the way
    test_create_without_apply_never_issues_a_write_statement does for the
    staged-create path."""
    role_id = _role()
    doc_id = _document(role_id)

    real_connect = dbmod.connect
    monkeypatch.setattr(dbmod, "connect", lambda *a, **kw: _WriteGuardedConn(real_connect(*a, **kw)))

    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 47 designers.\n"})
    assert r.status_code == 422


def test_the_refusal_is_worded_the_same_as_generation(client, sources):
    """Both paths enforce one rule, so they have to teach one lesson. If the
    save path grew its own message, the same mistake would read as two
    different rules depending on where you made it."""
    role_id = _role()
    doc_id = _document(role_id)
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 47 designers.\n"})
    expected = app.numeric_block_payload({"unmatched": ["47"]}, "cover_letter")
    assert r.get_json()["message"] == expected["message"]


def test_the_save_path_goes_through_the_shared_gate_function(client, sources, monkeypatch):
    """The test that fails if someone inlines a second copy of the gate
    sequence into the save route."""
    role_id = _role()
    doc_id = _document(role_id)

    calls = []
    real = app.run_fidelity_gates

    def spy(content_md, **kwargs):
        calls.append((content_md, kwargs))
        return real(content_md, **kwargs)

    monkeypatch.setattr(app, "run_fidelity_gates", spy)
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 12 designers in 2019.\n"})
    assert r.status_code == 200
    assert len(calls) == 1, "the save path must call run_fidelity_gates exactly once"
    assert calls[0][0] == "I led 12 designers in 2019."


def test_the_numeric_gate_itself_is_reached_on_save(client, sources, monkeypatch):
    """Belt to the spy's braces: stub the gate to block everything and the
    save must refuse, which can only happen if it actually ran."""
    role_id = _role()
    doc_id = _document(role_id)
    monkeypatch.setattr(ng, "check_numeric_facts",
                        lambda *a, **kw: {"blocked": True, "unmatched": ["stub"], "checked": 1})
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "Nothing numeric here at all.\n"})
    assert r.status_code == 422
    assert r.get_json()["unmatched"] == ["stub"]


# ------------------------------------------------------------------
# A save is a new version, never an overwrite
# ------------------------------------------------------------------

def test_a_save_inserts_a_new_version_and_leaves_the_parent_untouched(client, sources):
    role_id = _role()
    doc_id = _document(role_id)
    parent_before = _docs(role_id)[0]["content_md"]

    edited = "I led 12 designers across 3 countries. Shipped 2019.\n"
    r = client.post(f"/api/documents/{doc_id}/edit", json={"content_md": edited})
    assert r.status_code == 200

    rows = _docs(role_id)
    assert len(rows) == 2
    assert rows[0]["id"] == doc_id
    assert rows[0]["content_md"] == parent_before, "the parent row must be byte-identical"
    assert rows[1]["version"] == 2
    assert rows[1]["content_md"] == edited.strip()


def test_edited_from_is_null_on_generated_and_set_on_edited(client, sources):
    role_id = _role()
    doc_id = _document(role_id)
    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})

    rows = _docs(role_id)
    assert rows[0]["edited_from"] is None
    assert rows[1]["edited_from"] == doc_id


def test_model_is_null_on_an_edited_row(client, sources):
    """The column records which model produced the text. For a hand-edit the
    honest answer is none, and the UI names the author from edited_from."""
    role_id = _role()
    doc_id = _document(role_id)
    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})
    assert _docs(role_id)[1]["model"] is None


def test_an_edited_version_starts_as_draft_even_when_the_parent_was_approved(client, sources):
    """Approval is of a specific text. This is different text."""
    role_id = _role()
    doc_id = _document(role_id, status="approved")
    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})

    rows = _docs(role_id)
    assert rows[0]["status"] == "approved"
    assert rows[1]["status"] == "draft"


def test_version_numbering_continues_from_the_highest(client, sources):
    role_id = _role()
    _document(role_id, version=1)
    doc_id = _document(role_id, version=2)
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 12 designers in 2019.\n"})
    assert r.get_json()["version"] == 3


# ------------------------------------------------------------------
# Provenance recorded at save time, not inherited
# ------------------------------------------------------------------

def test_master_cv_sha_reflects_the_cv_at_save_time(client, sources):
    """If the master CV changed since the draft was generated, that's a real
    fact and the new version should record it rather than copying the
    parent's provenance forward."""
    import hashlib

    role_id = _role()
    stale = json.dumps({"flags": [], "gaps": [], "master_cv_file": "cv-leadership.md",
                        "master_cv_sha256": "0" * 64, "jd_sha256": "0" * 64,
                        "repairs": [], "numeric_gate": {"blocked": False, "unmatched": [],
                                                        "checked": 0}})
    doc_id = _document(role_id, critic_notes=stale)

    sources["master"].write_text(MASTER_CV + "Also grew revenue in 2021.\n")
    expected = hashlib.sha256(sources["master"].read_text().encode("utf-8")).hexdigest()

    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})

    fidelity = json.loads(_docs(role_id)[1]["critic_notes"])
    assert fidelity["master_cv_sha256"] == expected
    assert fidelity["master_cv_sha256"] != "0" * 64


def test_a_number_only_in_the_updated_master_cv_now_passes(client, sources):
    """The corpus is rebuilt from what's on disk at save time, so a number
    added to the master CV since generation is legitimately traceable."""
    role_id = _role()
    doc_id = _document(role_id)

    blocked = client.post(f"/api/documents/{doc_id}/edit",
                          json={"content_md": "I grew revenue 41% that year.\n"})
    assert blocked.status_code == 422

    sources["master"].write_text(MASTER_CV + "Grew revenue 41%.\n")
    ok = client.post(f"/api/documents/{doc_id}/edit",
                     json={"content_md": "I grew revenue 41% that year.\n"})
    assert ok.status_code == 200


def test_the_parents_language_gate_verdict_carries_forward(client, sources):
    """The language gate is a function of the JD and declared languages, not
    of this text — re-running it on an edit would re-decide the role instead
    of checking the edit. The verdict is still true of the role, so the
    edited version keeps it rather than losing the provenance."""
    role_id = _role()
    notes = json.dumps({"flags": [], "gaps": [], "repairs": [],
                        "language_gate": {"verdict": "warn", "overridden": False,
                                          "block": [], "warn": [{"language": "German"}],
                                          "note": []},
                        "numeric_gate": {"blocked": False, "unmatched": [], "checked": 0}})
    doc_id = _document(role_id, critic_notes=notes)
    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})

    fidelity = json.loads(_docs(role_id)[1]["critic_notes"])
    assert fidelity["language_gate"]["verdict"] == "warn"


# ------------------------------------------------------------------
# Scope and refusals
# ------------------------------------------------------------------

def test_a_cv_cannot_be_edited(client, sources):
    """A CV is structured content_json the renderer and the PDF depend on.
    Free-text editing would break the schema they read."""
    role_id = _role()
    doc_id = _document(role_id, doc_type="cv", content_json=json.dumps({"sections": []}))
    before = _count_docs()

    r = client.post(f"/api/documents/{doc_id}/edit", json={"content_md": "anything"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_editable"
    assert _count_docs() == before


def test_an_empty_edit_is_refused(client, sources):
    role_id = _role()
    doc_id = _document(role_id)
    r = client.post(f"/api/documents/{doc_id}/edit", json={"content_md": "   \n"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "empty"


def test_an_unchanged_edit_is_refused(client, sources):
    """Saving the stored text back would create a version identical to its
    parent — noise in a history whose job is showing what changed."""
    role_id = _role()
    doc_id = _document(role_id)
    r = client.post(f"/api/documents/{doc_id}/edit", json={"content_md": CLEAN_LETTER})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unchanged"


def test_editing_a_role_with_no_jd_is_refused(client, sources):
    """The numeric gate checks against the master CV, the JD and the
    profile. With no JD there is no corpus to check against."""
    role_id = _role(jd_text="")
    doc_id = _document(role_id)
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 12 designers.\n"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "no_jd"


def test_editing_a_role_with_no_mapped_cv_is_refused(client, sources):
    role_id = _role(category="Review")
    doc_id = _document(role_id)
    r = client.post(f"/api/documents/{doc_id}/edit",
                    json={"content_md": "I led 12 designers.\n"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "no_category"


def test_editing_a_missing_document_is_a_404(client, sources):
    assert client.post("/api/documents/99999/edit",
                       json={"content_md": "x"}).status_code == 404


# ------------------------------------------------------------------
# The list route surfaces the lineage
# ------------------------------------------------------------------

def test_the_documents_list_exposes_edited_from(client, sources):
    role_id = _role()
    doc_id = _document(role_id)
    client.post(f"/api/documents/{doc_id}/edit",
                json={"content_md": "I led 12 designers in 2019.\n"})

    docs = client.get(f"/api/roles/{role_id}/documents").get_json()["documents"]
    by_version = {d["version"]: d for d in docs}
    assert by_version[1]["edited_from"] is None
    assert by_version[2]["edited_from"] == doc_id
