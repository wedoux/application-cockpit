"""
_generate_cover_letter_plan_then_write (implementation passes 2 and 4).

Same fake-Anthropic-client convention as test_phase3a.py's CV retry tests
(_Usage/_Block/_Messages/_Client), extended to also produce a text response
for the write step alongside a tool_use response for the plan step, since
this orchestration makes both kinds of call.
"""

import generation
import prompt_assembly as pa


class _Usage:
    def __init__(self, in_tok=10, out_tok=10):
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class _Block:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _ToolResp:
    def __init__(self, data, stop_reason="tool_use"):
        self.content = [_Block("tool_use", input=data)] if data is not None else []
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _TextResp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block("text", text=text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class _Client:
    def __init__(self, responses):
        self.messages = _Messages(responses)


CV_TEXT = """
- Led design for AI features on a global wealth platform.
- Built a 700+ component system from scratch across 7 products.
"""

ROLE = {"company": "Acme", "title": "Senior Designer", "category": "Senior IC", "location": "Remote",
        "jd_text": "We need a hands-on senior designer who can ship AI features end to end "
                   "and has built a design system from zero."}
CONFIG = {"model": "claude-sonnet-5", "profile": {"name": "Jordan Reyes"}}
SYSTEM = "system prompt text"

GOOD_PLAN = {
    "opening_angle": "I want hands-on craft again.",
    "jd_requirements": ["ship AI features end to end", "built a design system from zero"],
    "jd_company_facts": [],
    "company_evidence": [],
    "proof_points": [
        {"claim": "I ship AI features end to end.", "jd_requirement": "ship AI features end to end",
         "cv_source_line": "Led design for AI features on a global wealth platform."},
        {"claim": "I build design systems from zero.", "jd_requirement": "built a design system from zero",
         "cv_source_line": "Built a 700+ component system from scratch across 7 products."},
    ],
    "differentiation": "I prototype in code, not just Figma.",
    "target_words": 300,
}


def _words(n):
    return " ".join(["word"] * n)


def test_happy_path_no_retries():
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 2
    assert result["retried"] is False
    assert result["plan_retried"] is False
    assert result["word_count_retried"] is False
    assert result["content_json"] == GOOD_PLAN
    assert result["content_md"] == _words(290)
    assert result["actual_words"] == 290


def test_plan_schema_failure_retries_once_then_succeeds():
    bad_plan = {"opening_angle": "x"}  # missing everything else
    client = _Client([_ToolResp(bad_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3
    assert result["plan_retried"] is True
    assert result["retried"] is True


def test_plan_fails_loudly_after_one_retry_and_never_reaches_the_write_step():
    bad_plan = {"opening_angle": "x"}
    client = _Client([_ToolResp(bad_plan), _ToolResp(bad_plan)])
    try:
        generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
        assert False, "expected GenerationError"
    except generation.GenerationError:
        pass
    assert client.messages.calls == 2, "no write-step call, and no third plan attempt"


def test_invalid_cv_source_line_triggers_a_plan_retry():
    """Schema-valid but the pointer doesn't trace to anything in the
    tailored CV, this is exactly the case cover_letter_schema.
    validate_source_lines exists to catch, and it must trigger the same
    retry path as a schema error, not silently pass through."""
    invented_plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": GOOD_PLAN["jd_requirements"][0],
         "cv_source_line": "Ran the entire company's rebrand single-handedly."},
        GOOD_PLAN["proof_points"][1],
    ]}
    client = _Client([_ToolResp(invented_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3
    assert result["plan_retried"] is True
    assert result["plan_retry_reason"] == "source_line"
    assert result["plan_retry_reasons"] == ["source_line"]


def test_invented_jd_requirement_triggers_a_plan_retry():
    """A jd_requirement that doesn't actually appear in the JD text, the
    pass 4 addition mirroring validate_source_lines in the other direction."""
    invented_plan = {**GOOD_PLAN, "jd_requirements": GOOD_PLAN["jd_requirements"] + ["can fly a plane"]}
    client = _Client([_ToolResp(invented_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3
    assert result["plan_retry_reason"] == "jd_requirement_trace"


def test_unmatched_proof_point_jd_pointer_triggers_a_plan_retry():
    """The direct fix for the ablation's finding: a proof point whose
    jd_requirement doesn't name anything the plan itself extracted."""
    bad_plan = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": "something never extracted as a requirement",
         "cv_source_line": GOOD_PLAN["proof_points"][0]["cv_source_line"]},
        GOOD_PLAN["proof_points"][1],
    ]}
    client = _Client([_ToolResp(bad_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3
    assert result["plan_retry_reason"] == "jd_requirement_pointer"


def test_schema_failure_reports_schema_as_the_retry_reason_not_source_line():
    bad_plan = {"opening_angle": "x"}  # missing everything else, a shape error
    client = _Client([_ToolResp(bad_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert result["plan_retry_reason"] == "schema"


def test_plan_retry_reason_is_none_when_no_retry_happens():
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert result["plan_retry_reason"] is None
    assert result["plan_retry_reasons"] == []


def test_source_line_validation_is_skipped_when_no_tailored_cv_exists():
    """No tailored CV yet (role has no stored CV, none generated this
    request), nothing to validate a pointer against, so the plan can't be
    rejected on that basis alone. verify_fidelity against the master CV is
    still the fallback safety net, same as always. jd_requirement checks
    still run, since those only need the JD, which is always present."""
    plan_with_any_pointer = {**GOOD_PLAN, "proof_points": [
        {"claim": "x", "jd_requirement": GOOD_PLAN["jd_requirements"][0],
         "cv_source_line": "Anything at all, unverifiable with no CV present."},
        GOOD_PLAN["proof_points"][1],
    ]}
    client = _Client([_ToolResp(plan_with_any_pointer), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, None)
    assert client.messages.calls == 2
    assert result["plan_retried"] is False


def test_write_step_over_target_retries_once_and_uses_the_corrected_draft():
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(_words(400)), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3
    assert result["word_count_retried"] is True
    assert result["retried"] is True
    assert result["actual_words"] == 290
    assert result["content_md"] == _words(290)


def test_write_step_still_over_after_retry_is_stored_not_blocked_or_looped():
    """Task 3's explicit instruction: if still over target after one retry,
    store it anyway and flag it. Never raise, never retry a third time."""
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(_words(400)), _TextResp(_words(380))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 3, "must not attempt a third write call"
    assert result["word_count_retried"] is True
    assert result["actual_words"] == 380
    assert result["content_md"] == _words(380)


def test_word_count_within_tolerance_does_not_retry():
    """target_words=300, default tolerance 10% -> threshold 330. 325 is over
    the bare target but within tolerance, so this must not retry."""
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(_words(325))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 2
    assert result["word_count_retried"] is False


def test_usage_and_cost_aggregate_across_every_call():
    client = _Client([
        _ToolResp({"opening_angle": "x"}),  # bad -> retried
        _ToolResp(GOOD_PLAN),
        _TextResp(_words(400)),  # over -> retried
        _TextResp(_words(290)),
    ])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
    assert client.messages.calls == 4
    assert result["usage"]["input_tokens"] == 40  # 4 calls x 10
    assert result["usage"]["output_tokens"] == 40
    assert result["cost_usd"] > 0


def test_refusal_on_plan_step_raises():
    client = _Client([_ToolResp(None, stop_reason="refusal")])
    try:
        generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
        assert False, "expected GenerationError"
    except generation.GenerationError:
        pass


def test_refusal_on_write_step_raises():
    client = _Client([_ToolResp(GOOD_PLAN), _TextResp(None, stop_reason="refusal")])
    try:
        generation._generate_cover_letter_plan_then_write(client, ROLE, CONFIG, SYSTEM, CV_TEXT)
        assert False, "expected GenerationError"
    except generation.GenerationError:
        pass


def test_cover_letter_targets_reads_per_category_config_with_fallback():
    cfg = {"cover_letter": {"word_targets": {"Leadership": 350}, "word_tolerance_pct": 15}}
    target, tolerance = generation._cover_letter_targets(cfg, "Leadership")
    assert target == 350
    assert tolerance == 15
    # A category with no explicit entry falls back to the module default,
    # not to some other category's number.
    target, tolerance = generation._cover_letter_targets(cfg, "Senior IC")
    assert target == generation.DEFAULT_TARGET_WORDS
    assert tolerance == 15  # tolerance is still read from config


def test_cover_letter_targets_falls_back_entirely_when_unconfigured():
    target, tolerance = generation._cover_letter_targets({}, "Senior IC")
    assert target == generation.DEFAULT_TARGET_WORDS
    assert tolerance == generation.DEFAULT_WORD_TOLERANCE_PCT


def test_proof_point_range_reads_per_category_config_with_fallback():
    import cover_letter_schema as cls
    cfg = {"cover_letter": {"proof_points": {"Leadership": {"min": 2, "max": 4}}}}
    lo, hi = generation._proof_point_range(cfg, "Leadership")
    assert (lo, hi) == (2, 4)
    lo, hi = generation._proof_point_range(cfg, "Senior IC")
    assert (lo, hi) == (cls.DEFAULT_MIN_PROOF_POINTS, cls.DEFAULT_MAX_PROOF_POINTS)


def test_proof_point_range_falls_back_entirely_when_unconfigured():
    import cover_letter_schema as cls
    lo, hi = generation._proof_point_range({}, "Senior IC")
    assert (lo, hi) == (cls.DEFAULT_MIN_PROOF_POINTS, cls.DEFAULT_MAX_PROOF_POINTS)


def test_configured_proof_point_range_is_actually_used_for_validation():
    """A plan with 3 proof points must be accepted when config allows up to
    3, and rejected (forcing a retry) when config caps at 2, proving the
    range is threaded through to validate_plan_json, not just read and
    ignored."""
    three_points_plan = {**GOOD_PLAN, "proof_points": GOOD_PLAN["proof_points"] + [
        {"claim": "y", "jd_requirement": GOOD_PLAN["jd_requirements"][1],
         "cv_source_line": GOOD_PLAN["proof_points"][1]["cv_source_line"]},
    ]}
    wide_config = {**CONFIG, "cover_letter": {"proof_points": {"Senior IC": {"min": 2, "max": 3}}}}
    client = _Client([_ToolResp(three_points_plan), _TextResp(_words(290))])
    result = generation._generate_cover_letter_plan_then_write(client, ROLE, wide_config, SYSTEM, CV_TEXT)
    assert client.messages.calls == 2
    assert result["plan_retried"] is False

    narrow_config = {**CONFIG, "cover_letter": {"proof_points": {"Senior IC": {"min": 2, "max": 2}}}}
    client2 = _Client([_ToolResp(three_points_plan), _ToolResp(GOOD_PLAN), _TextResp(_words(290))])
    result2 = generation._generate_cover_letter_plan_then_write(client2, ROLE, narrow_config, SYSTEM, CV_TEXT)
    assert result2["plan_retried"] is True
    assert result2["plan_retry_reason"] == "schema"


def test_generate_defaults_to_freeform_mode_when_unconfigured(monkeypatch):
    """The rollback safety net (Task 3's explicit ask): with no cover_letter
    config block at all, generate() must use the existing freeform path,
    not the new one, plan_then_write is opt-in."""
    calls = []
    monkeypatch.setattr(generation, "_generate_text", lambda client, a: calls.append("freeform") or {
        "content_md": "x", "content_json": None, "model": a["model"],
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read": 0, "cache_write": 0},
        "cost_usd": 0.0, "stop_reason": "end_turn", "truncated": False, "retried": False, "repairs": [],
    })
    monkeypatch.setattr(generation, "_generate_cover_letter_plan_then_write",
                         lambda *a, **kw: calls.append("plan_then_write") or {})
    monkeypatch.setattr(pa, "resolve_master_cv", lambda category, config: ("/fake/cv.md", "cv text"))
    monkeypatch.setattr(pa, "build_system_prompt", lambda config, master_cv, category=None: "sys")

    class _NoopClient:
        pass
    monkeypatch.setattr(generation.anthropic, "Anthropic", lambda: _NoopClient())

    generation.generate(ROLE, "cover_letter", config={"model": "claude-sonnet-5", "profile": {}})
    assert calls == ["freeform"]
