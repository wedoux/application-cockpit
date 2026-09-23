"""
cover_letter_experiment.py — verified against the isolated test DB, with
generation.generate mocked, before it is ever run for real. No test in this
file makes an Anthropic API call or touches the real cockpit.db (autouse
isolation from conftest.py covers that the same way it does everywhere
else in this suite).
"""

import cover_letter_experiment as exp
import db as dbmod
import generation


def _insert_role(company="Acme Corp", title="Senior Designer", category="Senior IC",
                  jd_text="A job description for a senior designer role."):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "jd_source, first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}-{category}", "manual", company, title, category, "submitted",
         jd_text, "paste", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def _insert_document(role_id, doc_type, content_md, version=1):
    conn = dbmod.connect()
    conn.execute(
        "INSERT INTO documents (role_id, doc_type, version, format, content_md, model, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (role_id, doc_type, version, "md", content_md, "test-model", "draft", dbmod.now()),
    )
    conn.commit()
    conn.close()


def _fake_generate_result(content_md="A generated letter.", company_evidence=None,
                           jd_company_facts=None, plan_retried=False, plan_retry_reason=None,
                           word_count_retried=False, cost_usd=0.01):
    return {
        "content_md": content_md,
        "content_json": {
            "opening_angle": "x",
            "jd_requirements": ["req a", "req b"],
            "jd_company_facts": jd_company_facts or [],
            "company_evidence": company_evidence or [],
            "proof_points": [{"claim": "a", "jd_requirement": "req a", "cv_source_line": "b"}] * 2,
            "differentiation": "d", "target_words": 300,
        },
        "model": "test-model",
        "usage": {"input_tokens": 10, "output_tokens": 10, "cache_read": 0, "cache_write": 0},
        "cost_usd": cost_usd,
        "stop_reason": "end_turn",
        "truncated": False,
        "retried": plan_retried or word_count_retried,
        "repairs": [],
        "plan_retried": plan_retried,
        "plan_retry_reason": plan_retry_reason,
        "plan_retry_reasons": [plan_retry_reason] if plan_retry_reason else [],
        "word_count_retried": word_count_retried,
    }


def _setup_role(company="Acme Corp", category="Senior IC",
                 cv_text="- A real CV bullet.", letter_text="An original letter with nothing much in it."):
    role_id = _insert_role(company=company, category=category)
    _insert_document(role_id, "cv", cv_text)
    _insert_document(role_id, "cover_letter", letter_text)
    return role_id


def test_run_experiment_scores_before_and_after_for_each_role(monkeypatch):
    _setup_role()
    monkeypatch.setattr(generation, "generate", lambda *a, **kw: _fake_generate_result())
    run = exp.run_experiment()
    assert len(run["results"]) == 1
    r = run["results"][0]
    assert "before" in r and "after" in r
    assert r["before"]["words"] == len("An original letter with nothing much in it.".split())
    assert r["after"]["words"] == len("A generated letter.".split())
    assert run["total_cost"] == 0.01
    assert run["stopped_early"] is False


def test_run_experiment_forces_plan_then_write_mode_regardless_of_config(monkeypatch):
    _setup_role()
    seen_configs = []

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        seen_configs.append(config)
        return _fake_generate_result()

    monkeypatch.setattr(generation, "generate", _gen)
    exp.run_experiment(config={"model": "claude-sonnet-5", "profile": {},
                                "cover_letter": {"mode": "freeform"}})
    assert seen_configs[0]["cover_letter"]["mode"] == "plan_then_write"


def test_run_experiment_passes_the_stored_cv_text_through(monkeypatch):
    _setup_role(cv_text="- The exact stored CV bullet.")
    seen = {}

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        seen["cv_text"] = tailored_cv_text
        return _fake_generate_result()

    monkeypatch.setattr(generation, "generate", _gen)
    exp.run_experiment()
    assert seen["cv_text"] == "- The exact stored CV bullet."


def test_a_failed_role_is_recorded_and_does_not_abort_the_rest(monkeypatch):
    _setup_role(company="A Corp")
    _setup_role(company="B Corp")

    def _gen(role, doc_type, config=None, tailored_cv_text=None):
        if role["company"] == "A Corp":
            raise generation.GenerationError("simulated failure")
        return _fake_generate_result()

    monkeypatch.setattr(generation, "generate", _gen)
    run = exp.run_experiment()
    assert len(run["results"]) == 2
    a_result = next(r for r in run["results"] if r["meta"]["company"] == "A Corp")
    b_result = next(r for r in run["results"] if r["meta"]["company"] == "B Corp")
    assert "error" in a_result and "after" not in a_result
    assert "after" in b_result and "error" not in b_result
    assert run["total_cost"] == 0.01  # only B's cost counts


def test_empty_company_evidence_is_recorded_on_the_after_row(monkeypatch):
    _setup_role()
    monkeypatch.setattr(generation, "generate",
                         lambda *a, **kw: _fake_generate_result(company_evidence=[]))
    run = exp.run_experiment()
    assert run["results"][0]["after"]["company_evidence_empty"] is True

    monkeypatch.setattr(generation, "generate",
                         lambda *a, **kw: _fake_generate_result(company_evidence=["Shipped X in Q2."]))
    run = exp.run_experiment()
    assert run["results"][0]["after"]["company_evidence_empty"] is False


def test_plan_retry_reason_is_carried_onto_the_after_row(monkeypatch):
    _setup_role()
    monkeypatch.setattr(generation, "generate", lambda *a, **kw: _fake_generate_result(
        plan_retried=True, plan_retry_reason="source_line"))
    run = exp.run_experiment()
    assert run["results"][0]["after"]["plan_retry_reason"] == "source_line"


def test_max_spend_stops_before_starting_a_role_that_would_exceed_it(monkeypatch):
    """The hard stop: checked before each role starts, not mid-call. Three
    roles at $0.02 each; a max_spend of $0.03 must process exactly two
    (0 -> 0.02 <= 0.03 starts; 0.02 -> 0.04 > 0.03 stops before the third)
    and report why."""
    for i in range(3):
        _setup_role(company=f"Company {i}")

    monkeypatch.setattr(generation, "generate",
                         lambda *a, **kw: _fake_generate_result(cost_usd=0.02))
    run = exp.run_experiment(max_spend=0.03)
    assert len(run["results"]) == 2
    assert all("after" in r for r in run["results"])
    assert run["stopped_early"] is True
    assert run["total_cost"] == 0.04
    assert "0.04" in run["stop_reason"] and "0.03" in run["stop_reason"]


def test_role_ids_restricts_the_run_to_a_named_subset(monkeypatch):
    """Task 5's cheap targeted run: only the named roles, not every stored
    cover letter."""
    role_a = _setup_role(company="A Corp")
    role_b = _setup_role(company="B Corp")
    _setup_role(company="C Corp")  # not in the requested subset
    monkeypatch.setattr(generation, "generate", lambda *a, **kw: _fake_generate_result())
    run = exp.run_experiment(role_ids=[role_a, role_b])
    assert len(run["results"]) == 2
    assert {r["role_id"] for r in run["results"]} == {role_a, role_b}


def test_max_spend_none_never_stops_early(monkeypatch):
    for i in range(3):
        _setup_role(company=f"Company {i}")
    monkeypatch.setattr(generation, "generate",
                         lambda *a, **kw: _fake_generate_result(cost_usd=100.0))
    run = exp.run_experiment(max_spend=None)
    assert len(run["results"]) == 3
    assert run["stopped_early"] is False


def test_build_report_preserves_the_criterion_section_verbatim():
    criterion = "# Pre-registered criterion\n\nFlip if X.\n\n---\n"
    run = {"results": [], "total_cost": 0.0, "stopped_early": False, "stop_reason": None}
    report = exp.build_report(run, criterion)
    assert report.startswith(criterion.rstrip())


def test_build_report_includes_counts_deltas_and_seam_proxies(monkeypatch):
    _setup_role(company="A Corp", letter_text="Short before letter here now.")
    monkeypatch.setattr(generation, "generate", lambda *a, **kw: _fake_generate_result(
        content_md="A different after letter that is longer than the before one was.",
        plan_retried=True, plan_retry_reason="source_line", word_count_retried=True,
        company_evidence=[], jd_company_facts=[],
    ))
    run = exp.run_experiment()
    report = exp.build_report(run, "# criterion\n\n---\n")
    assert "Plan retries forced by an invalid `cv_source_line` pointer: 1/1" in report
    assert "Write step needed the length retry: 1/1" in report
    assert "`company_evidence` came back empty: 1/1" in report
    assert "`jd_company_facts` came back empty: 1/1" in report
    assert "## Per-role deltas" in report
    assert "## Full per-paragraph paraphrase-duplication scores" in report
    assert "## Seam-risk proxies" in report
    assert "Mean sentence-length stdev" in report
    assert "Cross-letter opening repeats" in report
    assert "## Cross-letter phrase repetition (full-letter n-grams" in report
    assert "## Plans (full, per role)" in report


def test_build_report_lists_errors():
    run = {"results": [{"role_id": 1, "meta": {"company": "A Corp"}, "error": "boom"}],
           "total_cost": 0.0, "stopped_early": False, "stop_reason": None}
    report = exp.build_report(run, "# criterion\n\n---\n")
    assert "## Errors" in report
    assert "boom" in report


def test_build_fulltext_includes_only_the_three_named_roles(monkeypatch):
    role_ids = {}
    for company in ("Some Co", "Another Co"):
        role_ids[company] = _setup_role(company=company, letter_text=f"Before letter for {company}.")

    monkeypatch.setattr(generation, "generate", lambda *a, **kw: _fake_generate_result(
        content_md="After letter text here."))
    run = exp.run_experiment()

    # Force one of the two roles to be a "saved" one for this test, without
    # depending on the real production role_ids existing in the test DB.
    saved_id = run["results"][0]["role_id"]
    import cover_letter_experiment as exp_module
    original = exp_module.SAVED_FULL_TEXT_ROLE_IDS
    try:
        exp_module.SAVED_FULL_TEXT_ROLE_IDS = {saved_id}
        fulltext = exp_module.build_fulltext(run)
    finally:
        exp_module.SAVED_FULL_TEXT_ROLE_IDS = original

    assert f"role {saved_id}" in fulltext
    assert "Before letter for" in fulltext
    assert "After letter text here." in fulltext
    other_id = next(r["role_id"] for r in run["results"] if r["role_id"] != saved_id)
    assert f"role {other_id}" not in fulltext
