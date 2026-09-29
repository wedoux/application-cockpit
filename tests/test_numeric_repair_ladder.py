"""
The numeric repair ladder (rungs 2-4).
=======================================
Rung 1 is deterministic and lives in tests/test_numeric_fact_gate.py. This
file covers what happens when a number is still unmatched after it: one
small repair call, then one full regeneration, then storing the draft as
blocked rather than throwing it away.

The property that matters most here is the one that is easiest to lose: the
bar never moves. A repaired draft passes exactly the gates a fresh one does,
and a repair that fixes the number while breaking something else is a failed
repair, not a partial win.

Every API call is faked. conftest's no_live_api_calls fixture makes that
mandatory rather than merely customary — a test that forgets gets a
RealApiCallError instead of a bill.
"""
import json
import types

import pytest

import app
import db as dbmod
import generation
import numeric_repair

MASTER_CV = "Design system of 700+ components. Led a team of 7 designers."
JD = "We are a team of 35+ product designers."


def _insert_role(category="Leadership"):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("ladder-test", "manual", "Acme", "Head of Design", category, "sourced", JD,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _fake_generate(content_md, content_json=None):
    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        return {
            "content_md": content_md, "content_json": content_json,
            "master_cv_text": MASTER_CV, "master_cv_path": "/tmp/cv.md",
            "model": "test-model",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0},
            "cost_usd": 0.01, "stop_reason": "end_turn",
            "truncated": False, "retried": False, "repairs": [],
        }
    return _gen


class _FakeClient:
    """Returns a scripted emit_repaired_spans tool call, and counts how many
    times it was asked — the cap is only meaningful if something counts."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        spans = self.scripts.pop(0) if self.scripts else []
        usage = types.SimpleNamespace(
            input_tokens=40, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return types.SimpleNamespace(
            stop_reason="end_turn", usage=usage,
            content=[types.SimpleNamespace(type="tool_use", input={"spans": spans})])


def _use_client(monkeypatch, client):
    monkeypatch.setattr(generation, "client_factory", lambda: client)
    return client


def _docs(role_id):
    conn = dbmod.connect()
    rows = conn.execute(
        "SELECT * FROM documents WHERE role_id = ? ORDER BY id", (role_id,)).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------
# Rung 2 — the targeted repair
# ------------------------------------------------------------------

def test_a_successful_repair_stores_the_repaired_draft(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_generate("I led 7 designers. We shipped 42 features."))
    client = _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "We shipped many features."}]]))

    r = app.app.test_client().post(f"/api/roles/{role_id}/generate",
                                   json={"doc_types": ["cover_letter"]})
    assert r.status_code == 200
    doc = r.get_json()["documents"][0]
    assert doc["status"] == "draft"

    row = _docs(role_id)[0]
    assert "42" not in row["content_md"]
    assert "I led 7 designers." in row["content_md"], "the untouched sentence is untouched"
    assert len(client.calls) == 1, "one repair call, not a regeneration"


def test_the_repair_is_recorded_with_before_and_after(monkeypatch):
    """Nothing is fixed invisibly."""
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_generate("I led 7 designers. We shipped 42 features."))
    _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "We shipped many features."}]]))

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    notes = json.loads(_docs(role_id)[0]["critic_notes"])
    rewrites = [r for r in notes["repairs"] if r.get("kind") == "numeric-rewrite"]
    assert rewrites == [{"kind": "numeric-rewrite",
                         "before": "We shipped 42 features.",
                         "after": "We shipped many features."}]


def test_only_the_flagged_sentences_are_sent_to_the_model(monkeypatch):
    """The point of rung 2 is that it is cheap. Sending the whole draft back
    would make it a regeneration with extra steps."""
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate", _fake_generate(
        "I led 7 designers. A sentence with no numbers at all. We shipped 42 features."))
    client = _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "We shipped features."}]]))

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    sent = client.calls[0]["messages"][0]["content"]
    assert "We shipped 42 features." in sent
    assert "A sentence with no numbers at all" not in sent


def test_a_repair_that_leaves_the_number_unsourced_climbs_to_regeneration(monkeypatch):
    role_id = _insert_role()
    drafts = ["We shipped 42 features.", "We shipped 7 features."]

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        return _fake_generate(drafts.pop(0))(role, doc_type, config)

    monkeypatch.setattr(generation, "generate", _gen)
    # The repair hands back a number that is still not in the corpus.
    client = _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "We shipped 99 features."}]]))

    r = app.app.test_client().post(f"/api/roles/{role_id}/generate",
                                   json={"doc_types": ["cover_letter"]})
    assert r.get_json()["documents"][0]["status"] == "draft"
    row = _docs(role_id)[0]
    assert row["content_md"] == "We shipped 7 features.", "the regenerated draft is what stored"
    attempts = json.loads(row["critic_notes"])["numeric_attempts"]
    assert [a["action"] for a in attempts] == ["generate", "repair", "regenerate"]
    assert attempts[1]["outcome"] == "still_unmatched"


def test_a_repair_that_breaks_another_gate_is_a_failed_repair(monkeypatch):
    """Trading a caught problem for an uncaught one is not a repair. The
    rewrite fixes the number and introduces an unverifiable claim; the draft
    must be re-checked in full and the repair rejected."""
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_generate("We shipped 42 features."))
    _use_client(monkeypatch, _FakeClient([
        [{"id": 0, "text": "We shipped features at Northwind Robotics and Vantage Trust."}],
    ]))

    def _flagging_gates(content_md, **kwargs):
        fidelity = _real_gates(content_md, **kwargs)
        if "Northwind" in content_md:
            fidelity["flags"] = fidelity["flags"] + [{"kind": "entity", "text": "Northwind Robotics"}]
        return fidelity

    _real_gates = app.run_fidelity_gates
    monkeypatch.setattr(app, "run_fidelity_gates", _flagging_gates)

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    row = _docs(role_id)[0]
    assert "Northwind" not in row["content_md"], "the repair must not have been kept"
    attempts = json.loads(row["critic_notes"])["numeric_attempts"]
    assert attempts[1]["outcome"] == "broke_another_gate"


def test_the_cv_path_patches_json_and_re_renders(monkeypatch):
    """content_md is derived from content_json. Patching the render would be
    overwritten by the next one and would leave content_json disagreeing with
    the document."""
    cv_json = {"profile": "I shipped 42 features.", "what_i_lead": [], "experience": [],
               "projects": [], "education": [], "languages": []}
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_generate("I shipped 42 features.", content_json=cv_json))
    _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "I shipped a great deal."}]]))

    app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    row = _docs(role_id)[0]
    stored = json.loads(row["content_json"])
    assert stored["profile"] == "I shipped a great deal.", "the JSON is what got patched"
    assert "42" not in row["content_md"]
    assert "a great deal" in row["content_md"], "the markdown was re-rendered from the JSON"


# ------------------------------------------------------------------
# Rungs 3 and 4 — the cap, and keeping the draft
# ------------------------------------------------------------------

def test_the_attempt_cap_holds(monkeypatch):
    """One repair per draft and one regeneration per document. Four API calls
    is the ceiling: draft, repair, regenerate, repair."""
    role_id = _insert_role()
    generates = []

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        generates.append(doc_type)
        return _fake_generate("We shipped 42 features.")(role, doc_type, config)

    monkeypatch.setattr(generation, "generate", _gen)
    # Every repair hands back something still unsourced.
    client = _use_client(monkeypatch, _FakeClient([
        [{"id": 0, "text": "We shipped 99 features."}],
        [{"id": 0, "text": "We shipped 98 features."}],
        [{"id": 0, "text": "We shipped 97 features."}],
    ]))

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    assert len(generates) == 2, "one draft plus exactly one regeneration"
    assert len(client.calls) == 2, "one repair per draft, and no more"

    attempts = json.loads(_docs(role_id)[0]["critic_notes"])["numeric_attempts"]
    assert [a["action"] for a in attempts] == [
        "generate", "repair", "regenerate", "repair", "store_blocked"]


def test_every_attempt_records_what_it_cost(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate", _fake_generate("We shipped 42 features."))
    _use_client(monkeypatch, _FakeClient([[{"id": 0, "text": "We shipped 99 features."}]]))

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    attempts = json.loads(_docs(role_id)[0]["critic_notes"])["numeric_attempts"]
    billable = [a for a in attempts if a["action"] in ("generate", "repair", "regenerate")]
    assert billable, "something must have been billed"
    for attempt in billable:
        assert set(attempt["usage"]) == {"input_tokens", "output_tokens", "cache_read", "cache_write"}
        assert "cost_usd" in attempt


def test_a_blocked_cv_is_not_used_as_the_tailored_cv(monkeypatch):
    """A CV whose numbers never cleared the gate is not a CV a cover letter
    may build on."""
    role_id = _insert_role()
    seen = {}

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        seen[doc_type] = tailored_cv_text
        content = ("I shipped 42 features." if doc_type == "cv"
                   else "I led 7 designers.")
        return _fake_generate(content)(role, doc_type, config)

    monkeypatch.setattr(generation, "generate", _gen)
    _use_client(monkeypatch, _FakeClient([[], [], []]))  # no repair available

    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cv", "cover_letter"]})
    statuses = {row["doc_type"]: row["status"] for row in _docs(role_id)}
    assert statuses["cv"] == "blocked"
    assert statuses["cover_letter"] == "draft"
    assert seen["cover_letter"] is None, \
        "the blocked CV must not have been threaded into the letter"


def test_a_stored_blocked_draft_is_not_offered_as_the_tailored_cv_later(monkeypatch):
    """The exclusion is in the query, not just in the request that created
    it — a blocked CV sitting in the table from an earlier request must not
    be picked up by the next one either."""
    role_id = _insert_role()
    conn = dbmod.connect()
    ts = dbmod.now()
    conn.execute(
        """INSERT INTO documents (role_id, doc_type, version, format, content_md,
            model, critic_notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (role_id, "cv", 1, "md", "A blocked CV with 42 in it.", "m", None, "blocked", ts))
    conn.commit()
    conn.close()

    seen = {}

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        seen[doc_type] = tailored_cv_text
        return _fake_generate("I led 7 designers.")(role, doc_type, config)

    monkeypatch.setattr(generation, "generate", _gen)
    app.app.test_client().post(f"/api/roles/{role_id}/generate",
                               json={"doc_types": ["cover_letter"]})
    assert seen["cover_letter"] is None


# ------------------------------------------------------------------
# The edit path does not get the ladder
# ------------------------------------------------------------------

def test_a_hand_edit_with_an_unsourced_number_is_still_refused(monkeypatch):
    """The ladder is for model output. A hand edit that introduces an
    unsourced number is refused outright, with the same payload as before."""
    role_id = _insert_role()
    conn = dbmod.connect()
    ts = dbmod.now()
    doc_id = conn.execute(
        """INSERT INTO documents (role_id, doc_type, version, format, content_md,
            model, critic_notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (role_id, "cover_letter", 1, "md", "I led 7 designers.", "m", None, "draft", ts)
    ).lastrowid
    conn.commit()
    conn.close()

    called = []
    monkeypatch.setattr(generation, "repair_numbers",
                        lambda *a, **kw: called.append(1) or ([], {}, 0.0))

    r = app.app.test_client().post(f"/api/documents/{doc_id}/edit",
                                   json={"content_md": "I led 42 designers."})
    assert r.status_code == 422
    data = r.get_json()
    assert data["error"] == "numeric_fact_gate"
    assert "42" in data["unmatched"]
    assert called == [], "the edit path must never attempt a repair"


# ------------------------------------------------------------------
# The guard that makes all of the above honest
# ------------------------------------------------------------------

def test_a_test_cannot_reach_a_live_api_client():
    from conftest import RealApiCallError
    with pytest.raises(RealApiCallError):
        generation.client_factory()
