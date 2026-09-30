"""
Experience order: the timeline is fixed, tailoring happens inside it.
======================================================================
A recruiter reads a CV by its timeline and an ATS reconstructs career
history from it, so generation never reorders roles. Relevance is argued in
the profile and the capability section, which carry no timeline, and by
bullet selection and wording inside each role.

Found 2026-09-30: two master CV variants had a later role sitting below an
earlier one, and the generated CV inherited it. The master files were fixed
by hand; these tests are the part that stops it returning from the
generation side, at both ends — the master file and the draft.

Every company here is invented. Real employer names never enter a fixture.
"""
import json

import pytest

import app
import cv_order as co
import cv_schema
import db as dbmod
import generation

# Two master-CV heading formats, both real, told apart by the pipe.
HEAD_OF_DESIGN = """\
# A Candidate

## Profile

Some prose.

## Experience

### Head of Design — Acme Corp, Geneva
May 2022 – Feb 2026 · Platform

- Did a thing.

### Senior Consultant — Globex, Zurich
May 2021 – Apr 2022 · Banking

- Did another thing.

### Design Lead — Initech, London
Jun 2015 – May 2019 · Retail

- A third thing.

### Earlier

Roles before 2015, summarised.

## AI project

### Sidewinder — a side build (2026, ongoing)

- Not an employment role.

## Languages

English (native)
"""

ADVISORY = """\
# A Candidate

## Profile

Some prose.

## Selected engagements

### Acme Corp — Head of Design | Geneva
May 2022 – Feb 2026 · Platform

- Did a thing.

### Globex — Senior Consultant | Zurich
May 2021 – Apr 2022 · Banking

- Did another thing.

### Earlier

Summarised.
"""


# ------------------------------------------------------------------
# Parsing both master formats
# ------------------------------------------------------------------

def _dated(text):
    return [r for r in co.parse_master_roles(text) if not r["earlier"]]


def test_parses_the_role_company_city_format():
    roles = _dated(HEAD_OF_DESIGN)
    assert [r["company"] for r in roles] == ["Acme Corp", "Globex", "Initech"]
    assert [r["start"] for r in roles] == [(2022, 5), (2021, 5), (2015, 6)]


def test_parses_the_company_role_city_format():
    roles = _dated(ADVISORY)
    assert [r["company"] for r in roles] == ["Acme Corp", "Globex"]
    assert [r["start"] for r in roles] == [(2022, 5), (2021, 5)]


def test_the_ai_project_is_not_a_role_and_earlier_is_only_a_sentinel():
    """The AI project lives in its own section and must never be read as
    employment. "### Earlier" is in the experience section and does belong
    to the timeline, but as an undated trailing sentinel, never as a dated
    role — it has no company of its own."""
    roles = co.parse_master_roles(HEAD_OF_DESIGN)
    assert not any("Sidewinder" in (r["company"] or "") for r in roles)
    assert [r["company"] for r in roles if not r["earlier"]] == \
        ["Acme Corp", "Globex", "Initech"]
    assert [r["earlier"] for r in roles] == [False, False, False, True]
    assert roles[-1]["company"] is None


# ------------------------------------------------------------------
# The master file's own order
# ------------------------------------------------------------------

def test_a_master_cv_in_order_passes():
    co.check_master_order(HEAD_OF_DESIGN, "cv-example.md")  # does not raise


def test_a_master_cv_out_of_order_is_rejected_by_file_and_roles():
    """The real 2026-09-30 shape: a 2021-22 role sitting below a 2015-19
    one. The message has to name the file and both roles, because the fix
    belongs in the source."""
    out_of_order = HEAD_OF_DESIGN.replace(
        """### Senior Consultant — Globex, Zurich
May 2021 – Apr 2022 · Banking

- Did another thing.

### Design Lead — Initech, London
Jun 2015 – May 2019 · Retail

- A third thing.
""",
        """### Design Lead — Initech, London
Jun 2015 – May 2019 · Retail

- A third thing.

### Senior Consultant — Globex, Zurich
May 2021 – Apr 2022 · Banking

- Did another thing.
""")
    with pytest.raises(co.MasterOrderError) as exc:
        co.check_master_order(out_of_order, "cv-example.md")
    message = str(exc.value)
    assert "cv-example.md" in message
    assert "Globex" in message and "Initech" in message


def test_an_undated_entry_does_not_invent_an_ordering():
    """A null date is a legitimate state in this codebase. Guessing an order
    for one would be the same class of error as guessing the date."""
    undated = HEAD_OF_DESIGN.replace("Jun 2015 – May 2019 · Retail", "Retail")
    co.check_master_order(undated, "cv-example.md")  # does not raise
    assert [r["start"] for r in co.parse_master_roles(undated)][-1] is None


# ------------------------------------------------------------------
# Comparing a draft against the master
# ------------------------------------------------------------------

MASTER = co.parse_master_roles(HEAD_OF_DESIGN)


def _exp(*companies):
    return [{"company": c, "role": "R", "dates": "d", "context": "c", "bullets": ["b"]}
            for c in companies]


def test_matching_order_is_ok():
    verdict = co.compare_experience_order(_exp("Acme Corp", "Globex", "Initech"), MASTER)
    assert verdict["status"] == "ok"


def test_a_swapped_pair_is_a_reorder():
    verdict = co.compare_experience_order(_exp("Globex", "Acme Corp", "Initech"), MASTER)
    assert verdict["status"] == "reordered"
    assert verdict["order"] == ["Acme Corp", "Globex", "Initech"]


def test_restore_order_puts_them_back_without_touching_content():
    drafted = _exp("Initech", "Acme Corp", "Globex")
    drafted[0]["bullets"] = ["a distinctive bullet"]
    restored = co.restore_order(drafted, MASTER)
    assert [e["company"] for e in restored] == ["Acme Corp", "Globex", "Initech"]
    assert restored[2]["bullets"] == ["a distinctive bullet"], "content is untouched"


def test_a_city_suffix_on_a_draft_company_still_matches():
    """The model writes "Acme Corp, Geneva" into the company field while the
    master heading parses down to "Acme Corp". Normalising only one side
    made every role read as simultaneously missing and invented, and that
    fired on a correct CV the first time this ran for real."""
    drafted = _exp("Acme Corp, Geneva", "Globex, Zurich", "Initech, London")
    assert co.compare_experience_order(drafted, MASTER)["status"] == "ok"
    assert co.draft_company("Acme Corp, Geneva") == co.draft_company("Acme Corp")


def test_a_city_suffix_does_not_hide_a_genuinely_different_company():
    verdict = co.compare_experience_order(
        _exp("Acme Corp, Geneva", "Hooli, Palo Alto", "Initech, London"), MASTER)
    assert verdict["status"] == "mismatch"
    assert any("Hooli" in r for r in verdict["reasons"])


# ------------------------------------------------------------------
# The Earlier block: a trailing, undated sentinel
# ------------------------------------------------------------------

EARLIER_MASTER = HEAD_OF_DESIGN.replace(
    "### Earlier\n\nRoles before 2015, summarised.",
    "### Earlier\n\nFounder, Umbrella Co (a thing). UX Lead, Vandelay (another). "
    "Architect, Stark Industries (a third).")
EARLIER_ROLES = co.parse_master_roles(EARLIER_MASTER)


def test_the_earlier_block_is_parsed_as_a_trailing_sentinel():
    assert EARLIER_ROLES[-1]["earlier"] is True
    assert EARLIER_ROLES[-1]["start"] is None
    assert "Umbrella Co" in EARLIER_ROLES[-1]["text"]
    assert [r["company"] for r in EARLIER_ROLES if not r["earlier"]] == \
        ["Acme Corp", "Globex", "Initech"]


def test_an_earlier_entry_matched_and_last_is_ok():
    drafted = _exp("Acme Corp", "Globex", "Initech",
                   "Umbrella Co / Vandelay / Stark Industries")
    assert co.compare_experience_order(drafted, EARLIER_ROLES)["status"] == "ok"


def test_an_earlier_entry_promoted_above_a_dated_role_is_flagged():
    """A summary of several old roles sitting mid-sequence means the model
    misread the structure. Worth seeing, not worth silently sorting away."""
    drafted = _exp("Acme Corp", "Umbrella Co / Vandelay", "Globex", "Initech")
    verdict = co.compare_experience_order(drafted, EARLIER_ROLES)
    assert verdict["status"] == "mismatch"
    assert any("Earlier summary is not last" in r for r in verdict["reasons"])


def test_an_earlier_entry_naming_an_unknown_company_is_flagged():
    """Without this, a role invented inside the Earlier summary would slip
    through — it matches no dated role, so it would otherwise be waved
    past as "that's just the Earlier block"."""
    drafted = _exp("Acme Corp", "Globex", "Initech", "Umbrella Co / Hooli")
    verdict = co.compare_experience_order(drafted, EARLIER_ROLES)
    assert verdict["status"] == "mismatch"
    assert any("Earlier line" in r and "Hooli" in r for r in verdict["reasons"])


def test_an_earlier_entry_with_its_companies_reversed_is_flagged():
    drafted = _exp("Acme Corp", "Globex", "Initech",
                   "Stark Industries / Vandelay / Umbrella Co")
    verdict = co.compare_experience_order(drafted, EARLIER_ROLES)
    assert verdict["status"] == "mismatch"
    assert any("out of order" in r for r in verdict["reasons"])


def test_a_master_with_no_earlier_block_still_rejects_an_extra_entry():
    drafted = _exp("Acme Corp", "Globex", "Initech", "Hooli / Umbrella Co")
    verdict = co.compare_experience_order(drafted, MASTER)
    assert verdict["status"] == "mismatch"
    assert any("not in the master CV" in r for r in verdict["reasons"])


def test_earlier_sorts_last_when_the_dated_roles_are_restored():
    drafted = _exp("Globex", "Acme Corp", "Initech", "Umbrella Co / Vandelay")
    restored = co.restore_order(drafted, EARLIER_ROLES)
    assert [e["company"] for e in restored] == [
        "Acme Corp", "Globex", "Initech", "Umbrella Co / Vandelay"]


def test_an_added_role_is_a_mismatch_not_a_reorder():
    verdict = co.compare_experience_order(_exp("Acme Corp", "Hooli", "Globex"), MASTER)
    assert verdict["status"] == "mismatch"
    assert any("Hooli" in r for r in verdict["reasons"])


def test_a_dropped_role_is_a_mismatch():
    """The rule is that generation never adds, drops, merges or reorders.
    A draft carrying fewer roles than the master is therefore a fidelity
    question — which roles belong in this CV — and not something to resolve
    by sorting what is left."""
    verdict = co.compare_experience_order(_exp("Acme Corp", "Globex"), MASTER)
    assert verdict["status"] == "mismatch"
    assert any("missing from the draft: Initech" in r for r in verdict["reasons"])


def test_a_duplicated_role_is_a_mismatch():
    verdict = co.compare_experience_order(_exp("Acme Corp", "Acme Corp", "Globex"), MASTER)
    assert verdict["status"] == "mismatch"


# ------------------------------------------------------------------
# Wired into the generate route
# ------------------------------------------------------------------

def _insert_role():
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("order-test", "manual", "Hooli", "Head of Design", "Leadership", "sourced",
         "A job description with no numbers.", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _cv_json(*companies):
    return {"profile": "A profile.", "what_i_lead": [], "experience": _exp(*companies),
            "projects": [], "education": [], "languages": []}


def _fake_cv_generate(cv_json, master=HEAD_OF_DESIGN, counter=None):
    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        if counter is not None:
            counter.append(doc_type)
        return {
            "content_md": cv_schema.cv_json_to_markdown(cv_json),
            "content_json": cv_json,
            "master_cv_text": master, "master_cv_path": "/tmp/cv-example.md",
            "model": "test-model",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read": 0, "cache_write": 0},
            "cost_usd": 0.0, "stop_reason": "end_turn",
            "truncated": False, "retried": False, "repairs": [],
        }
    return _gen


def _stored(role_id):
    conn = dbmod.connect()
    row = conn.execute(
        "SELECT * FROM documents WHERE role_id = ? ORDER BY id DESC LIMIT 1", (role_id,)
    ).fetchone()
    conn.close()
    return row


def test_a_swapped_draft_is_restored_without_a_second_api_call(monkeypatch):
    role_id = _insert_role()
    calls = []
    monkeypatch.setattr(generation, "generate",
                        _fake_cv_generate(_cv_json("Globex", "Acme Corp", "Initech"),
                                          counter=calls))
    monkeypatch.setattr(app.pa, "resolve_master_cv",
                        lambda cat, cfg: ("/tmp/cv-example.md", HEAD_OF_DESIGN))

    r = app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    assert r.status_code == 200
    row = _stored(role_id)
    stored = json.loads(row["content_json"])
    assert [e["company"] for e in stored["experience"]] == ["Acme Corp", "Globex", "Initech"]
    assert calls == ["cv"], "restoring the order must not cost a regeneration"

    notes = json.loads(row["critic_notes"])
    order_repairs = [x for x in notes["repairs"] if x.get("kind") == "experience-order"]
    assert len(order_repairs) == 1
    assert order_repairs[0]["restored"] == "Acme Corp, Globex, Initech"


def test_the_rendered_markdown_is_re_rendered_from_the_reordered_json(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_cv_generate(_cv_json("Globex", "Acme Corp", "Initech")))
    monkeypatch.setattr(app.pa, "resolve_master_cv",
                        lambda cat, cfg: ("/tmp/cv-example.md", HEAD_OF_DESIGN))

    app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    md = _stored(role_id)["content_md"]
    assert md.index("Acme Corp") < md.index("Globex"), \
        "the stored markdown must match the stored JSON, not the pre-sort order"


def test_a_draft_with_a_different_role_set_is_flagged_not_reordered(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_cv_generate(_cv_json("Globex", "Hooli")))
    monkeypatch.setattr(app.pa, "resolve_master_cv",
                        lambda cat, cfg: ("/tmp/cv-example.md", HEAD_OF_DESIGN))

    app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    row = _stored(role_id)
    stored = json.loads(row["content_json"])
    assert [e["company"] for e in stored["experience"]] == ["Globex", "Hooli"], \
        "a set mismatch must not be quietly resolved by sorting"

    notes = json.loads(row["critic_notes"])
    flags = [f for f in notes["flags"] if f["kind"] == "experience-set"]
    assert len(flags) == 1
    assert "Hooli" in flags[0]["text"]
    assert not [x for x in notes["repairs"] if x.get("kind") == "experience-order"]


def test_an_out_of_order_master_blocks_generation_before_any_call(monkeypatch):
    role_id = _insert_role()
    calls = []
    monkeypatch.setattr(generation, "generate",
                        _fake_cv_generate(_cv_json("Acme Corp"), counter=calls))
    out_of_order = HEAD_OF_DESIGN.replace(
        "### Head of Design — Acme Corp, Geneva\nMay 2022 – Feb 2026 · Platform",
        "### Head of Design — Acme Corp, Geneva\nMay 2012 – Feb 2016 · Platform")
    monkeypatch.setattr(app.pa, "resolve_master_cv",
                        lambda cat, cfg: ("/tmp/cv-example.md", out_of_order))

    r = app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    assert r.status_code == 422
    data = r.get_json()
    assert data["error"] == "master_cv_order"
    assert "cv-example.md" in data["message"]
    assert calls == [], "nothing may be generated against an out-of-order master"


def test_a_matching_order_records_no_repair(monkeypatch):
    role_id = _insert_role()
    monkeypatch.setattr(generation, "generate",
                        _fake_cv_generate(_cv_json("Acme Corp", "Globex", "Initech")))
    monkeypatch.setattr(app.pa, "resolve_master_cv",
                        lambda cat, cfg: ("/tmp/cv-example.md", HEAD_OF_DESIGN))

    app.app.test_client().post(f"/api/roles/{role_id}/generate", json={"doc_types": ["cv"]})
    notes = json.loads(_stored(role_id)["critic_notes"])
    assert not [x for x in notes["repairs"] if x.get("kind") == "experience-order"]
    assert not [f for f in notes["flags"] if f["kind"] == "experience-set"]


# ------------------------------------------------------------------
# The prompt itself
# ------------------------------------------------------------------

def test_the_cv_prompt_no_longer_tells_the_model_to_reorder_experience():
    import prompt_assembly as pa
    assert "reorder and reweight to lead with what's most" not in pa.CV_TASK
    assert "Rules: reorder and reweight to fit the JD" not in pa.CV_TASK


def test_the_cv_prompt_states_the_fixed_order_rule():
    import prompt_assembly as pa
    assert "in EXACTLY the order the master CV lists" in pa.CV_TASK
    assert "Never reorder, add, drop or merge roles" in pa.CV_TASK
    assert "the experience timeline is fixed" in pa.CV_TASK


def test_the_cv_prompt_points_relevance_at_the_sections_built_for_it():
    import prompt_assembly as pa
    assert "THIS is where cross-role relevance goes" in pa.CV_TASK
    assert "fits THIS role" in pa.CV_TASK
    assert "ordered by relevance precisely because they carry no timeline" in pa.CV_TASK
