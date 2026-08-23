import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfReader

import app
import cv_render
import db as dbmod
import pdf_export
import prompt_assembly as pa


def _browser_available():
    try:
        with pdf_export.sync_playwright() as p:
            pdf_export._launch(p).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _browser_available(),
    reason="no usable browser for PDF export — install Google Chrome, or run "
           "`playwright install chromium` in this project's .venv",
)


def _pdftotext_page_text(pdf_path, page_num):
    """Extract text for a single page via poppler's pdftotext, not pypdf.
    pypdf was tried first and rejected: against this project's embedded
    variable Inter font it doesn't just garble numbers, it silently drops
    entire text runs (a whole bullet list vanished from extract_text() on a
    page that visually and via pdftotext clearly had it) — unusable as a
    per-page content-amount signal here."""
    result = subprocess.run(
        ["pdftotext", "-layout", "-f", str(page_num), "-l", str(page_num), str(pdf_path), "-"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _long_cv_content_json(n_experience):
    """A CV long enough to span multiple pages — self-contained, not the real
    master CV, so this test doesn't depend on files outside the repo. Built to
    exercise exactly the failure mode caught in Phase 3c: a short tail section
    (Languages) stranded alone on a near-empty trailing page. n_experience
    controls total length, so the caller can sweep it — whether a short tail
    section lands awkwardly at a page boundary depends on exactly how much
    content precedes it, so a single fixed length can miss the bug entirely
    (verified: one fixed length passed even with the Phase 3c bug reproduced)."""
    bullets = [
        "Led a cross-functional team through a major platform redesign, cutting "
        "time-to-market by roughly a third while improving stakeholder alignment.",
        "Ran a multi-month research program with dozens of expert users, feeding "
        "findings directly into product decisions and roadmap prioritisation.",
        "Built and governed a design system adopted across most product teams, "
        "reducing rework and improving cross-team consistency.",
        "Partnered with engineering and product leadership to embed design "
        "earlier in delivery, tightening specs and reducing review cycles.",
    ]
    experience = [
        {"role": f"Senior Role {i}", "company": f"Company {i}", "logo_key": None,
         "dates": f"20{15+i} – 20{16+i}", "context": "A regulated enterprise platform.",
         "bullets": bullets}
        for i in range(n_experience)
    ]
    return {
        "profile": (
            "Senior product design leader with many years building information-dense, "
            "expert-user interfaces for regulated and enterprise platforms. Works "
            "directly with engineers and product managers on product judgment, and has "
            "stood up design functions and governed design systems from zero. Thinks in "
            "workflows and mental models, not just screens."
        ),
        "what_i_lead": [
            {"label": "Complex workflows", "detail": "Information-dense interfaces for expert users under time pressure."},
            {"label": "Research with real users", "detail": "Discovery and validation with expert practitioners, not proxies."},
            {"label": "Design systems", "detail": "Built and governed systems that get adopted, not just shipped."},
            {"label": "Cross-team influence", "detail": "Aligning product, engineering, and commercial teams without formal authority."},
        ],
        "experience": experience,
        "projects": [
            {"name": "Side Project", "context": "A personal build, ongoing.",
             "bullets": ["Designed and built a working prototype end to end.",
                         "Used it to sharpen how I specify and critique AI-assisted work."]},
        ],
        "education": [
            {"qualification": "MA Interactive Media", "institution": "A University", "dates": "2010"},
            {"qualification": "BA (Hons) Graphic Design", "institution": "Another University", "dates": "2007"},
        ],
        "languages": [
            {"language": "English", "level": "Full professional"},
            {"language": "Greek", "level": "Native"},
            {"language": "French", "level": "Professional working"},
            {"language": "Spanish", "level": "Limited working"},
        ],
    }


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler's pdftotext not installed")
@pytest.mark.parametrize("n_experience", range(3, 8))
def test_pdf_pagination_never_strands_a_widow_page(tmp_path, n_experience):
    """A page break must never leave a near-empty trailing page — the Phase 3c
    bug where the Languages section landed alone at the bottom of its own
    page after the rest of the CV filled the ones before it. Swept across
    several content lengths (n_experience) rather than one fixed length,
    since whether the tail lands awkwardly at a page boundary depends on
    exactly how much precedes it.

    Range is 3-7 roles — realistic tailored-CV length (a typical master
    CV runs 3-5 roles). Known, deliberately out of scope: an 8-role stress
    case still reproduces a milder version of this (Languages alone, but
    correctly labeled and intact, not a headless orphaned line) — fixing
    that too would require either merging Education+Languages into one
    unbreakable tail block (rejected: relocates the problem to a fuller,
    still mostly-empty page 3) or dynamically measuring and shrinking
    content per generation (rejected: crosses the auto-shrink-to-fit line).
    A real tradeoff, flagged rather than silently patched around."""
    cfg = pa.load_config()
    html = cv_render.render_cv_html(_long_cv_content_json(n_experience), "Leadership", cfg)
    out = tmp_path / f"long_cv_{n_experience}.pdf"
    pdf_export.html_to_pdf(html, out)

    n_pages = len(PdfReader(str(out)).pages)
    if n_pages < 2:
        return  # too short to reach a page break at all — nothing to strand

    MIN_CHARS_PER_PAGE = 300
    for i in range(1, n_pages + 1):
        text_len = len(_pdftotext_page_text(out, i))
        assert text_len >= MIN_CHARS_PER_PAGE, (
            f"n_experience={n_experience}: page {i} of {n_pages} has only "
            f"{text_len} extracted characters — looks like a stranded widow "
            f"page, not a genuine last page of content"
        )


def test_pdf_export_produces_nonempty_file_with_expected_page_count(tmp_path):
    cfg = pa.load_config()
    html = cv_render.render_cover_letter_html(
        "Dear team,\n\nI am writing to apply for this role. Short letter, one page.\n\nBest,\nJordan",
        "Leadership", cfg,
    )
    out = tmp_path / "letter.pdf"
    pdf_export.html_to_pdf(html, out)
    assert out.exists() and out.stat().st_size > 1000
    assert len(PdfReader(str(out)).pages) == 1


def _insert_cv_document(company, title):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, jd_text, "
        "jd_source, first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}", "manual", company, title, "Leadership", "sourced",
         "A job description.", "paste", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    content_json = json.dumps({
        "profile": f"Profile for {company}.",
        "what_i_lead": [{"label": "A", "detail": "B"}],
        "experience": [{"role": title, "company": company, "logo_key": None,
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


def test_download_generates_on_demand_and_caches(monkeypatch):
    doc_id = _insert_cv_document("Acme Corp", "Head of Design")

    calls = {"n": 0}
    real_html_to_pdf = pdf_export.html_to_pdf

    def counting_html_to_pdf(html, path):
        calls["n"] += 1
        return real_html_to_pdf(html, path)

    monkeypatch.setattr(pdf_export, "html_to_pdf", counting_html_to_pdf)

    client = app.app.test_client()
    r1 = client.get(f"/api/documents/{doc_id}/download")
    assert r1.status_code == 200
    assert r1.content_type == "application/pdf"
    assert calls["n"] == 1

    conn = dbmod.connect()
    row = conn.execute("SELECT file_path, format FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    assert row["format"] == "pdf"
    assert row["file_path"] and Path(row["file_path"]).exists()

    r2 = client.get(f"/api/documents/{doc_id}/download")
    assert r2.status_code == 200
    assert calls["n"] == 1  # second request served the cached file, no regeneration


def test_download_serves_the_right_file_for_the_right_document_id():
    doc_a = _insert_cv_document("Acme Corp", "Head of Design")
    doc_b = _insert_cv_document("Globex Inc", "Senior IC")

    client = app.app.test_client()
    client.get(f"/api/documents/{doc_a}/download")
    client.get(f"/api/documents/{doc_b}/download")

    conn = dbmod.connect()
    path_a = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_a,)).fetchone()["file_path"]
    path_b = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_b,)).fetchone()["file_path"]
    conn.close()

    assert path_a != path_b
    assert Path(path_a).exists() and Path(path_b).exists()
    assert "acme" in path_a.lower() and "globex" in path_b.lower()
