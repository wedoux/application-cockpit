import base64
import tempfile
from pathlib import Path

import pytest

import cv_render
import prompt_assembly

MINIMAL_CV = {
    "profile": "A minimal profile.",
    "what_i_lead": ["Thing one"],  # old flat-string shape — see test_legacy_flat_what_i_lead_still_renders
    "experience": [{
        "role": "Role", "company": "Company", "logo_key": None,
        "dates": "2020", "context": "Context", "bullets": ["Did a thing"],
    }],
    "education": [{"qualification": "BA", "institution": "Uni", "dates": "2007"}],
    "languages": [{"language": "English", "level": "C2"}],
}


def _fake_config(assets_dir, logo_map=None):
    fonts = Path(assets_dir) / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / "InterVariable.woff2").write_bytes(b"not-a-real-woff2-but-bytes-are-enough")
    (fonts / "InterVariable-Italic.woff2").write_bytes(b"not-a-real-woff2-but-bytes-are-enough")
    return {
        "paths": {"assets": str(assets_dir)},
        "company_logo_map": logo_map or {},
        "profile": {
            "name": "Test Person", "email": "test@example.com", "phone": "+1 555 0100",
            "location": "Testville", "linkedin": "linkedin.com/in/testperson",
            "headshot": None,
            "taglines": {"Leadership": "Test Leadership Tagline"},
        },
    }


def test_render_minimal_valid_json_no_crash():
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(MINIMAL_CV, "Leadership", _fake_config(d))
    assert "<html" in html
    assert "Role" in html and "Company" in html


def test_null_logo_key_renders_cleanly():
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(MINIMAL_CV, "Leadership", _fake_config(d))
    assert "<img" not in html  # no headshot, no logo — nothing to disturb alignment
    assert 'class="exp-title"' in html


def test_legacy_flat_what_i_lead_still_renders():
    """what_i_lead moved from a flat string per item to {label, detail} so the
    template can bold the label. Existing DB rows were generated under the old
    shape and must keep rendering — no crash, no bold prefix, just the text."""
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(MINIMAL_CV, "Leadership", _fake_config(d))
    assert "Thing one" in html
    assert '<p class="lead-item">Thing one</p>' in html  # no <b> prefix for the old shape


def test_structured_what_i_lead_gets_bold_label():
    cv = {**MINIMAL_CV, "what_i_lead": [{"label": "Design organisations", "detail": "Building functions from zero."}]}
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(cv, "Leadership", _fake_config(d))
    assert "<b>Design organisations:</b> Building functions from zero." in html


def test_projects_section_omitted_when_absent():
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(MINIMAL_CV, "Leadership", _fake_config(d))
    assert ">Projects<" not in html  # section header absent entirely


def test_projects_section_renders_when_present():
    cv = {**MINIMAL_CV, "projects": [{
        "name": "Field Notes", "context": "Two-agent reasoning QA system",
        "bullets": ["Built a Research Copilot and a QA Validation Agent."],
    }]}
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(cv, "Leadership", _fake_config(d))
    assert ">Projects<" in html
    assert "Field Notes" in html and "Research Copilot" in html


def test_education_grouped_two_per_line():
    cv = {**MINIMAL_CV, "education": [
        {"qualification": "MA Interactive Media", "institution": "UAL", "dates": "2010"},
        {"qualification": "BA Graphic Design", "institution": "Solent", "dates": "2007"},
        {"qualification": "UX for AI Certification", "institution": "UXforAI", "dates": "2026"},
    ]}
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(cv, "Leadership", _fake_config(d))
    assert html.count('class="edu-item"') == 2  # 3 entries -> 2 lines (2 + 1)
    assert "MA Interactive Media" in html and "BA Graphic Design" in html
    # first two qualifications share one line, joined by ". "
    import re
    line = re.search(r'<p class="edu-item">(.*?)</p>', html).group(1)
    assert "MA Interactive Media" in line and "BA Graphic Design" in line


def test_null_dates_render_no_date_line_and_no_inferred_boundary():
    """Never infer a boundary like 'Pre-2015' — null dates means no date line."""
    cv = {**MINIMAL_CV, "experience": [{
        "role": "Earlier Role", "company": "Old Co", "logo_key": None,
        "dates": None, "context": "", "bullets": ["Did something."],
    }]}
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(cv, "Leadership", _fake_config(d))
    assert "Pre-2015" not in html
    assert 'class="exp-meta"' not in html  # no meta line at all — nothing to show


def test_missing_font_fails_loudly():
    with tempfile.TemporaryDirectory() as d:
        try:
            cv_render.render_cv_html(MINIMAL_CV, "Leadership", {
                "paths": {"assets": d}, "company_logo_map": {}, "profile": {},
            })
            assert False, "expected RenderError for a missing Inter font"
        except cv_render.RenderError:
            pass


def test_inter_embedded_no_synthesized_small_caps():
    with tempfile.TemporaryDirectory() as d:
        html = cv_render.render_cv_html(MINIMAL_CV, "Leadership", _fake_config(d))
    assert "@font-face" in html and "'Inter'" in html
    assert "font-variant: small-caps" not in html
    assert "font-variant:small-caps" not in html
    # tabular numerals so date ranges align
    assert '"tnum"' in html


def test_every_schema_field_appears_in_rendered_html():
    with tempfile.TemporaryDirectory() as d:
        assets = Path(d)
        (assets / "logo-acme.png").write_bytes(b"not-a-real-png-but-bytes-are-enough")
        cfg = _fake_config(assets, logo_map={"Acme Corp": "logo-acme.png"})
        cv = {
            "profile": "UNIQUE_PROFILE_MARKER",
            "what_i_lead": [{"label": "UNIQUE_LEAD_LABEL_MARKER", "detail": "UNIQUE_LEAD_DETAIL_MARKER"}],
            "experience": [{
                "role": "UNIQUE_ROLE_MARKER", "company": "Acme Corp", "logo_key": "Acme Corp",
                "dates": "UNIQUE_DATES_MARKER", "context": "UNIQUE_CONTEXT_MARKER",
                "bullets": ["UNIQUE_BULLET_MARKER"],
            }],
            "projects": [{"name": "UNIQUE_PROJECT_NAME_MARKER", "context": "UNIQUE_PROJECT_CONTEXT_MARKER",
                          "bullets": ["UNIQUE_PROJECT_BULLET_MARKER"]}],
            "education": [{"qualification": "UNIQUE_QUAL_MARKER",
                           "institution": "UNIQUE_INSTITUTION_MARKER", "dates": "UNIQUE_EDU_DATES"}],
            "languages": [{"language": "UNIQUE_LANGUAGE_MARKER", "level": "UNIQUE_LEVEL_MARKER"}],
        }
        html = cv_render.render_cv_html(cv, "Leadership", cfg)
    for marker in ("UNIQUE_PROFILE_MARKER", "UNIQUE_LEAD_LABEL_MARKER", "UNIQUE_LEAD_DETAIL_MARKER",
                   "UNIQUE_ROLE_MARKER", "Acme Corp", "UNIQUE_DATES_MARKER", "UNIQUE_CONTEXT_MARKER",
                   "UNIQUE_BULLET_MARKER", "UNIQUE_PROJECT_NAME_MARKER", "UNIQUE_PROJECT_CONTEXT_MARKER",
                   "UNIQUE_PROJECT_BULLET_MARKER", "UNIQUE_QUAL_MARKER", "UNIQUE_INSTITUTION_MARKER",
                   "UNIQUE_EDU_DATES", "UNIQUE_LANGUAGE_MARKER", "UNIQUE_LEVEL_MARKER"):
        assert marker in html, f"{marker!r} missing from rendered HTML"
    # logo_key resolved to a real asset -> embedded as a data URI, not a broken ref
    assert "data:" in html and "base64" in html


def test_header_comes_from_config_not_hardcoded():
    with tempfile.TemporaryDirectory() as d:
        cfg_a = _fake_config(d)
        html_a = cv_render.render_cv_html(MINIMAL_CV, "Leadership", cfg_a)
        cfg_b = _fake_config(d)
        cfg_b["profile"]["name"] = "A Totally Different Name"
        cfg_b["profile"]["email"] = "different@example.org"
        html_b = cv_render.render_cv_html(MINIMAL_CV, "Leadership", cfg_b)
    assert "Test Person" in html_a and "A Totally Different Name" not in html_a
    assert "A Totally Different Name" in html_b and "different@example.org" in html_b
    assert "Test Leadership Tagline" in html_a


def test_no_personal_data_hardcoded_in_template_or_renderer():
    """Tier 2 boundary: a peer swaps config.yaml, not the template or this
    module. Forbidden values are read from the real, gitignored config.yaml
    rather than hardcoded here — this test file is committed, and a hardcoded
    real name/email/phone in it would be exactly the leak it's meant to
    guard against. Skips cleanly on a fresh clone with no config.yaml yet."""
    try:
        config = prompt_assembly.load_config()
    except FileNotFoundError:
        pytest.skip("no local config.yaml — nothing to check")
    profile = config.get("profile", {})
    forbidden = [v for v in (
        profile.get("name"), profile.get("email"), profile.get("linkedin"),
        profile.get("location"), profile.get("phone"),
    ) if v]
    template_src = Path(__file__).parent.parent / "templates" / "cv.html"
    renderer_src = Path(__file__).parent.parent / "cv_render.py"
    for path in (template_src, renderer_src):
        text = path.read_text(encoding="utf-8")
        for s in forbidden:
            assert s not in text, f"{s!r} hardcoded in {path.name}"
