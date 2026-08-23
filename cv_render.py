#!/usr/bin/env python3
"""
CV HTML rendering (Phase 3b).
=============================
Pure presentation: reads content_json only, never content_md, never alters
content. All personal data (name, contact, taglines, headshot) comes from
config.yaml's profile: block — nothing personal is hardcoded here or in
the template, which is the Tier 2 boundary (a peer swaps config, not this
code, to render their own CV).

Images are embedded as base64 data URIs so the rendered HTML makes no
external requests and needs no static file route — the same string works for
the /preview endpoint and, unchanged, for the Phase 3b PDF export step. The
Inter variable font (assets/fonts/) is embedded the same way via @font-face —
not a CDN, not a system font. It's a design reference typeface now, not a
pixel match for the old externally-produced reference PDFs (those used
Liberation Sans from a pipeline this build replaces).

Role and company are separate fields in the schema; this module joins them
with an em dash for display. That's the only place an em dash exists in a
CV — never in generated text, only here in the template.
"""

import base64
import mimetypes
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


class RenderError(Exception):
    pass


def _data_uri(path):
    """Return a data: URI for an existing file, or None. Missing assets (a
    bad logo_key, a headshot that hasn't been added yet) degrade to "no
    image" rather than a broken render or a crash."""
    if path is None:
        return None
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _font_face_css(assets_dir):
    """@font-face rules for the embedded Inter variable font, both weight
    (100-900, one file covers the whole range) and italic. Raises if the
    font isn't where it's expected — a missing font should fail loudly, not
    silently fall back to a system face and drift from the design."""
    regular = _data_uri(assets_dir / "fonts" / "InterVariable.woff2")
    italic = _data_uri(assets_dir / "fonts" / "InterVariable-Italic.woff2")
    if not regular or not italic:
        raise RenderError(
            "Inter variable font missing from assets/fonts/ — expected "
            "InterVariable.woff2 and InterVariable-Italic.woff2"
        )
    return (
        "@font-face { font-family: 'Inter'; font-style: normal; font-weight: 100 900; "
        f"font-display: block; src: url({regular}) format('woff2'); }}\n"
        "@font-face { font-family: 'Inter'; font-style: italic; font-weight: 100 900; "
        f"font-display: block; src: url({italic}) format('woff2'); }}"
    )


def _normalize_lead_item(item):
    """what_i_lead moved from a flat string per item to {label, detail} so the
    template can reproduce the reference's bold-label style. Existing rows in
    the DB were generated under the old shape and still need to render —
    label=None tells the template to skip the bold prefix rather than show a
    blank label."""
    if isinstance(item, dict):
        return {"label": item.get("label") or None, "detail": item.get("detail", "")}
    return {"label": None, "detail": item}


def _header_context(category, config):
    """Context shared by both templates' header block (name, tagline, contact,
    headshot, embedded font) — the "reuse the CV header block" requirement for
    the cover-letter letterhead is this function, not copy-pasted markup."""
    profile = config.get("profile") or {}
    assets_dir = Path(config["paths"]["assets"])
    headshot_uri = _data_uri(assets_dir / profile["headshot"]) if profile.get("headshot") else None
    return {
        "profile": profile,
        "tagline": (profile.get("taglines") or {}).get(category, ""),
        "headshot_uri": headshot_uri,
        "font_face_css": _font_face_css(assets_dir),
    }


def render_cv_html(content_json, category, config):
    """Render content_json (the validated CV JSON — see cv_schema.py) to a
    single self-contained HTML string."""
    if not isinstance(content_json, dict):
        raise RenderError("content_json must be a dict — nothing to render")

    ctx = _header_context(category, config)
    assets_dir = Path(config["paths"]["assets"])

    def asset_uri(filename):
        return _data_uri(assets_dir / filename) if filename else None

    what_i_lead = [_normalize_lead_item(item) for item in content_json.get("what_i_lead", [])]

    logo_map = config.get("company_logo_map") or {}
    experience = []
    for e in content_json.get("experience", []):
        logo_key = e.get("logo_key")
        logo_uri = asset_uri(logo_map.get(logo_key)) if logo_key else None
        meta = " · ".join(
            x.strip() for x in (e.get("dates") or "", e.get("context") or "") if x and x.strip()
        )
        experience.append({**e, "logo_uri": logo_uri, "meta": meta})

    projects = content_json.get("projects") or []

    education = content_json.get("education", [])
    education_pairs = [education[i:i + 2] for i in range(0, len(education), 2)]

    language_line = " · ".join(
        f"{l.get('language', '')} ({l.get('level', '')})" for l in content_json.get("languages", [])
    )

    template = _env.get_template("cv.html")
    return template.render(
        cv=content_json,
        what_i_lead=what_i_lead,
        experience=experience,
        projects=projects,
        education_pairs=education_pairs,
        language_line=language_line,
        **ctx,
    )


def render_cover_letter_html(content_md, category, config):
    """Render a cover letter's prose (content_md — cover letters have no
    structured JSON, text stays the primary format) as an optional styled
    letterhead PDF source. Reuses the same header block and template system
    as the CV; never the default output, only built on request."""
    if not isinstance(content_md, str) or not content_md.strip():
        raise RenderError("content_md must be a non-empty string — nothing to render")

    ctx = _header_context(category, config)
    template = _env.get_template("cover_letter.html")
    return template.render(body=content_md.strip(), **ctx)


if __name__ == "__main__":
    # Manual smoke render against a role already in the DB.
    import sys
    import json as _json
    import db as dbmod
    import prompt_assembly as pa

    doc_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = dbmod.connect()
    if doc_id:
        row = conn.execute(
            "SELECT d.content_json, r.category FROM documents d JOIN roles r ON r.id = d.role_id "
            "WHERE d.id = ? AND d.doc_type = 'cv'", (doc_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT d.content_json, r.category FROM documents d JOIN roles r ON r.id = d.role_id "
            "WHERE d.doc_type = 'cv' AND d.content_json IS NOT NULL ORDER BY d.id DESC LIMIT 1",
        ).fetchone()
    conn.close()
    if not row or not row["content_json"]:
        print("No CV document with content_json found.")
        sys.exit(1)
    cfg = pa.load_config()
    html = render_cv_html(_json.loads(row["content_json"]), row["category"], cfg)
    out = Path(__file__).parent / "cv_preview_smoke.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(html)} chars)")
