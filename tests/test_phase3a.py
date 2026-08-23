import cv_schema
import generation
import verifier

GOOD_CV = {
    "profile": "Design leader with a track record in regulated platforms.",
    "what_i_lead": [
        {"label": "Design organisations", "detail": "Building functions from zero."},
        {"label": "Product strategy", "detail": "Turning ambiguity into decision models."},
    ],
    "experience": [{
        "role": "Head of Design", "company": "Acme Corp", "logo_key": None,
        "dates": "2019 - 2022", "context": "Enterprise platform",
        "bullets": ["Led a team of 5 designers across 3 products."],
    }],
    "education": [{"qualification": "BA", "institution": "Uni", "dates": "2007"}],
    "languages": [{"language": "English", "level": "C2"}],
}


def test_validate_cv_json_rejects_malformed():
    errors = cv_schema.validate_cv_json({"profile": "only this key"})
    assert errors
    assert any("experience" in e for e in errors)


def test_validate_cv_json_accepts_well_formed():
    assert cv_schema.validate_cv_json(GOOD_CV) == []


def test_repair_recovers_xml_tag_leak():
    """Observed bug: the model wraps array contents in XML <item> tags as a
    single string (echoing structured-XML language about an unrelated
    project elsewhere in about-me.md) instead of a real JSON array."""
    leaked = {
        "profile": "x",
        "what_i_lead": ("<item><label>A</label><detail>Detail A</detail></item>"
                        "<item><label>B</label><detail>Detail B</detail></item>"),
        "experience": ("<item><role>R</role><company>C</company><logo_key>null</logo_key>"
                       "<dates>2020</dates><context>Ctx</context>"
                       "<bullets><item>Did X</item></bullets></item>"),
        "education": "<item><qualification>BA</qualification><institution>Uni</institution><dates>2007</dates></item>",
        "languages": "<item><language>English</language><level>C2</level></item>",
    }
    repaired, repairs = cv_schema.repair_malformed_arrays(leaked)
    assert cv_schema.validate_cv_json(repaired) == []
    assert repaired["what_i_lead"] == [{"label": "A", "detail": "Detail A"}, {"label": "B", "detail": "Detail B"}]
    assert repaired["experience"][0]["bullets"] == ["Did X"]
    assert {r["field"] for r in repairs} == {"what_i_lead", "experience", "education", "languages"}
    assert all(r["shape"] == "xml_tags" for r in repairs)


def test_repair_recovers_bracket_less_json_leak():
    """A second observed shape: the model drops the outer [...] and emits the
    array's contents as a bare comma-separated string."""
    leaked = {
        "profile": "x",
        "what_i_lead": '{"label":"A","detail":"Detail A"}, {"label":"B","detail":"Detail B"}',
        "experience": '{"role":"R","company":"C","logo_key":null,"dates":"2020","context":"Ctx","bullets":["Did X"]}',
        "education": '{"qualification":"BA","institution":"Uni","dates":"2007"}',
        "languages": '{"language":"English","level":"C2"}',
    }
    repaired, repairs = cv_schema.repair_malformed_arrays(leaked)
    assert cv_schema.validate_cv_json(repaired) == []
    assert repaired["what_i_lead"] == [{"label": "A", "detail": "Detail A"}, {"label": "B", "detail": "Detail B"}]
    assert all(r["shape"] == "bracket_less_json" for r in repairs)


def test_repair_does_not_silently_empty_unrecognized_strings():
    """The dangerous failure mode: a field that doesn't match either repair
    pattern must be left exactly as-is, so it still fails validation and
    surfaces as a real error — not silently coerced to an empty (schema-valid)
    list, which would hide content loss instead of catching it."""
    data = {"profile": "x", "what_i_lead": "Just some prose, not a list at all.",
            "experience": [], "education": [], "languages": []}
    repaired, repairs = cv_schema.repair_malformed_arrays(data)
    assert repaired["what_i_lead"] == "Just some prose, not a list at all."
    assert any("what_i_lead" in e for e in cv_schema.validate_cv_json(repaired))
    assert repairs == []


def _all_strings(data):
    if isinstance(data, str):
        return [data] if data else []
    if isinstance(data, list):
        return [s for v in data for s in _all_strings(v)]
    if isinstance(data, dict):
        return [s for v in data.values() for s in _all_strings(v)]
    return []


def test_markdown_projection_is_lossless():
    """Every string value in the JSON — including logo_key, easy to forget since
    it isn't a prose field — must survive into content_md verbatim. The verifier
    only reads content_md, so anything dropped here is an invisible blind spot."""
    cv = {**GOOD_CV, "experience": [{**GOOD_CV["experience"][0], "logo_key": "ACME-LOGO-KEY-XYZ"}]}
    md = cv_schema.cv_json_to_markdown(cv)
    for s in _all_strings(cv):
        assert s in md, f"{s!r} missing from the markdown projection"


class _Usage:
    def __init__(self):
        self.input_tokens = 10
        self.output_tokens = 10


class _Block:
    def __init__(self, type_, input_):
        self.type = type_
        self.input = input_


class _Resp:
    def __init__(self, data):
        self.content = [_Block("tool_use", data)]
        self.stop_reason = "tool_use"
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


_A = {"model": "claude-sonnet-5", "system": "sys", "user": "user"}


def test_cv_retry_fires_once_then_succeeds():
    client = _Client([_Resp({"profile": "incomplete"}), _Resp(GOOD_CV)])
    result = generation._generate_cv(client, _A)
    assert client.messages.calls == 2
    assert result["retried"] is True
    assert result["content_json"] == GOOD_CV


def test_cv_fails_loudly_after_one_retry():
    client = _Client([_Resp({"profile": "bad"}), _Resp({"profile": "still bad"})])
    try:
        generation._generate_cv(client, _A)
        assert False, "expected GenerationError"
    except generation.GenerationError:
        pass
    assert client.messages.calls == 2  # no third attempt


def test_cv_repair_metadata_flows_through_generate():
    """A repaired field must be recorded on the result, not just fixed in
    place — app.py stores this in critic_notes so a repair is never invisible."""
    leaked_cv = {**GOOD_CV, "what_i_lead": "<item><label>A</label><detail>Da</detail></item>"
                                            "<item><label>B</label><detail>Db</detail></item>"}
    client = _Client([_Resp(leaked_cv)])
    result = generation._generate_cv(client, _A)
    assert client.messages.calls == 1  # repaired without needing a retry
    assert result["retried"] is False
    assert result["repairs"] == [{"field": "what_i_lead", "shape": "xml_tags", "recovered": 2}]
    assert result["content_json"]["what_i_lead"] == [{"label": "A", "detail": "Da"}, {"label": "B", "detail": "Db"}]


MASTER_FIXTURE = """
### Head of Design — Acme Corp
2019 - 2022 · Enterprise platform
- Led a team of 5 designers across 3 products.
- Cut onboarding time by 20%.
"""


def test_verifier_flags_invented_specific():
    generated = ("**Head of Design — Acme Corp**\n*2019 - 2022*\n"
                 "- Founded the design system and led a team of 12 designers.\n")
    result = verifier.verify_fidelity(generated, MASTER_FIXTURE)
    assert any(f["text"] == "12" for f in result["flags"])


def test_verifier_quiet_on_clean_fixture():
    generated = "**Head of Design — Acme Corp**\n*2019 - 2022*\n- Led a team of 5 designers across 3 products.\n"
    result = verifier.verify_fidelity(generated, MASTER_FIXTURE)
    assert result["flags"] == []
