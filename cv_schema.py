#!/usr/bin/env python3
"""
CV structured-content schema (Phase 3a, spec section 7).
==========================================================
The CV generation call is forced (Anthropic tool use) to emit this shape
instead of Markdown, so there is no fence-stripping and the shape is
predictable for the future PDF template (Phase 3b). validate_cv_json() is the
second line of defence: tool use guarantees valid JSON syntax, not that every
field is the right type or present — that's what generation.py's retry-once
loop checks against.

repair_malformed_arrays() handles two observed failure modes where an array
field comes back as a string instead of a real JSON array: XML-style <item>
tags (an echo of "structured XML" language elsewhere in the candidate's
about-me content, describing an unrelated project, which has nothing to
do with this tool call), and JSON content missing its outer brackets
(comma-separated quoted values/objects with no enclosing [...]). The
content itself is usually fine, just mis-shaped, so this recovers it
deterministically instead of spending a retry on it. Both repair
strategies only fire on a positive match for their specific pattern — an
unrecognized string is left untouched, not silently emptied, so it still
surfaces as a validation error rather than disappearing.
"""

import json
import xml.etree.ElementTree as ET

CV_TOOL_NAME = "emit_cv"

CV_TOOL = {
    "name": CV_TOOL_NAME,
    "description": "Emit the tailored CV as structured content. Call this exactly once with the complete document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "profile": {"type": "string",
                        "description": "2-4 sentence positioning paragraph, tailored to this role."},
            "what_i_lead": {
                "type": "array",
                "description": ("A JSON array of {label, detail} objects, one per capability line. "
                                 "label is a short 2-4 word heading (e.g. 'Design organisations'); "
                                 "detail is the supporting description. NOT a string. NOT XML. No "
                                 "<item> tags or any other markup — each array element is a plain "
                                 "JSON object."),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["label", "detail"],
                },
            },
            "experience": {
                "type": "array",
                "description": ("A JSON array of objects, one per role. NOT a string. NOT XML. No "
                                 "<item> tags or any other markup — each array element is a plain "
                                 "JSON object with the fields below."),
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "company": {"type": "string"},
                        "logo_key": {"type": ["string", "null"],
                                     "description": "Exact company_logo_map key, or null if none."},
                        "dates": {"type": ["string", "null"],
                                  "description": ("The date range as stated in the master CV. If the "
                                                   "master CV gives no explicit date for this entry, "
                                                   "use null — never infer or approximate a boundary "
                                                   "(e.g. do not write 'Pre-2015' or similar).")},
                        "context": {"type": "string"},
                        "bullets": {
                            "type": "array", "items": {"type": "string"},
                            "description": ("A JSON array of strings, one per bullet. NOT a string. "
                                             "NOT XML. No <item> tags — plain array elements only."),
                        },
                    },
                    "required": ["role", "company", "logo_key", "dates", "context", "bullets"],
                },
            },
            "projects": {
                "type": "array",
                "description": ("Optional. A JSON array of {name, context, bullets} objects for "
                                 "standalone projects (e.g. an open-source tool, an AI/technical "
                                 "side build) that don't fit the employment-history shape. Omit "
                                 "the field, or use an empty array, "
                                 "if there's nothing that belongs here — never invent one. Populate "
                                 "only from what the master CV states, same traceability rule as "
                                 "everything else."),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "context": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "context", "bullets"],
                },
            },
            "education": {
                "type": "array",
                "description": "A JSON array of objects, one per qualification. NOT a string, NOT XML.",
                "items": {
                    "type": "object",
                    "properties": {
                        "qualification": {"type": "string"},
                        "institution": {"type": "string"},
                        "dates": {"type": "string"},
                    },
                    "required": ["qualification", "institution", "dates"],
                },
            },
            "languages": {
                "type": "array",
                "description": "A JSON array of objects, one per language. NOT a string, NOT XML.",
                "items": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string"},
                        "level": {"type": "string"},
                    },
                    "required": ["language", "level"],
                },
            },
        },
        "required": ["profile", "what_i_lead", "experience", "education", "languages"],
    },
}


def _parse_xml_items(s):
    """Parse a "<item>...</item><item>...</item>" string into a list of
    Elements, tolerating nested tags. Returns None — not [] — whenever s isn't
    a string, has no <item> tag at all, or parses to zero items: a plain
    string with no angle brackets is trivially "valid XML" with no children,
    so without the explicit tag check this would misfire on any ordinary
    string and silently empty it instead of leaving it alone."""
    if not isinstance(s, str) or "<item" not in s:
        return None
    try:
        root = ET.fromstring(f"<root>{s}</root>")
    except ET.ParseError:
        return None
    items = root.findall("item")
    return items or None


def _text(el, tag):
    child = el.find(tag) if el is not None else None
    return (child.text or "").strip() if child is not None and child.text else ""


def _try_json_array(s, item_type):
    """If s is JSON content missing its outer brackets (comma-separated
    values/objects with no enclosing [...]), or is itself valid JSON typed as
    a string, parse it into a real list. Returns None if it doesn't produce a
    list of the expected item type."""
    if not isinstance(s, str) or not s.strip():
        return None
    for candidate in (s.strip(), f"[{s.strip()}]"):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list) and all(isinstance(x, item_type) for x in parsed):
            return parsed
    return None


def _repair_str_list_field(data, field, repairs):
    raw = data.get(field)
    if not isinstance(raw, str):
        return
    arr = _try_json_array(raw, str)
    if arr is not None:
        data[field] = arr
        repairs.append({"field": field, "shape": "bracket_less_json", "recovered": len(arr)})
        return
    items = _parse_xml_items(raw)
    if items is not None:
        data[field] = [(el.text or "").strip() for el in items]
        repairs.append({"field": field, "shape": "xml_tags", "recovered": len(items)})


def _repair_obj_list_field(data, field, from_xml_items, repairs):
    raw = data.get(field)
    if not isinstance(raw, str):
        return
    arr = _try_json_array(raw, dict)
    if arr is not None:
        data[field] = arr
        repairs.append({"field": field, "shape": "bracket_less_json", "recovered": len(arr)})
        return
    items = _parse_xml_items(raw)
    if items is not None:
        result = from_xml_items(items)
        data[field] = result
        repairs.append({"field": field, "shape": "xml_tags", "recovered": len(result)})


def repair_malformed_arrays(data):
    """Best-effort repair for array fields the model returned as a string
    instead of a real JSON array (XML <item> tags, or JSON missing its outer
    brackets). Returns (data, repairs): a new dict, plus a list of
    {field, shape, recovered} describing what fired — empty if nothing needed
    repair. Fields that don't match either pattern are left untouched — never
    emptied — so they still surface as a normal validation error and feed the
    retry loop instead of disappearing. A repair reshapes model output that
    passed the API's own JSON syntax check but not the intended schema, so
    it's recorded rather than applied silently — see generation.py and
    app.py, which store this in critic_notes and the UI surfaces it."""
    if not isinstance(data, dict):
        return data, []
    data = dict(data)
    repairs = []

    def _null_coalesce(v):
        return None if v in ("", "null", "None", None) else v

    def what_i_lead_from_xml(items):
        return [{"label": _text(el, "label"), "detail": _text(el, "detail")} for el in items]
    _repair_obj_list_field(data, "what_i_lead", what_i_lead_from_xml, repairs)

    def experience_from_xml(items):
        exp = []
        for el in items:
            bullets_el = el.find("bullets")
            bullets = [(b.text or "").strip() for b in bullets_el.findall("item")] if bullets_el is not None else []
            exp.append({
                "role": _text(el, "role"), "company": _text(el, "company"),
                "logo_key": _null_coalesce(_text(el, "logo_key")),
                "dates": _null_coalesce(_text(el, "dates")),
                "context": _text(el, "context"), "bullets": bullets,
            })
        return exp
    _repair_obj_list_field(data, "experience", experience_from_xml, repairs)

    def projects_from_xml(items):
        proj = []
        for el in items:
            bullets_el = el.find("bullets")
            bullets = [(b.text or "").strip() for b in bullets_el.findall("item")] if bullets_el is not None else []
            proj.append({"name": _text(el, "name"), "context": _text(el, "context"), "bullets": bullets})
        return proj
    _repair_obj_list_field(data, "projects", projects_from_xml, repairs)

    for field, subfields in (("education", ("qualification", "institution", "dates")),
                              ("languages", ("language", "level"))):
        _repair_obj_list_field(
            data, field,
            lambda items, sf=subfields: [{s: _text(el, s) for s in sf} for el in items],
            repairs,
        )

    return data, repairs


def _err(errors, path, msg):
    errors.append(f"{path}: {msg}")


def _check_str(v, path, errors):
    if not isinstance(v, str) or not v.strip():
        _err(errors, path, "expected a non-empty string")


def _check_str_list(v, path, errors):
    if not isinstance(v, list):
        _err(errors, path, "expected a list of strings")
        return
    for i, item in enumerate(v):
        _check_str(item, f"{path}[{i}]", errors)


def _check_nullable_str(v, path, errors):
    if v is not None and (not isinstance(v, str) or not v.strip()):
        _err(errors, path, "expected a non-empty string or null")


def validate_cv_json(data):
    """Return a list of error strings; empty list means valid."""
    errors = []
    if not isinstance(data, dict):
        return ["root: expected a JSON object"]

    _check_str(data.get("profile"), "profile", errors)

    lead = data.get("what_i_lead")
    if not isinstance(lead, list):
        _err(errors, "what_i_lead", "expected a list of {label, detail} objects")
    else:
        for i, item in enumerate(lead):
            p = f"what_i_lead[{i}]"
            if not isinstance(item, dict):
                _err(errors, p, "expected an object with label and detail")
                continue
            _check_str(item.get("label"), f"{p}.label", errors)
            _check_str(item.get("detail"), f"{p}.detail", errors)

    exp = data.get("experience")
    if not isinstance(exp, list) or not exp:
        _err(errors, "experience", "expected a non-empty list")
    else:
        for i, e in enumerate(exp):
            p = f"experience[{i}]"
            if not isinstance(e, dict):
                _err(errors, p, "expected an object")
                continue
            for field in ("role", "company", "context"):
                _check_str(e.get(field), f"{p}.{field}", errors)
            if "dates" not in e:
                _err(errors, f"{p}.dates", "key must be present (use null if there's no date)")
            else:
                _check_nullable_str(e["dates"], f"{p}.dates", errors)
            if "logo_key" not in e or (e["logo_key"] is not None and not isinstance(e["logo_key"], str)):
                _err(errors, f"{p}.logo_key", "expected a string or null")
            _check_str_list(e.get("bullets"), f"{p}.bullets", errors)

    if "projects" in data and data["projects"] is not None:
        projects = data["projects"]
        if not isinstance(projects, list):
            _err(errors, "projects", "expected a list (or omit the field)")
        else:
            for i, pr in enumerate(projects):
                p = f"projects[{i}]"
                if not isinstance(pr, dict):
                    _err(errors, p, "expected an object")
                    continue
                for field in ("name", "context"):
                    _check_str(pr.get(field), f"{p}.{field}", errors)
                _check_str_list(pr.get("bullets"), f"{p}.bullets", errors)

    edu = data.get("education")
    if not isinstance(edu, list):
        _err(errors, "education", "expected a list")
    else:
        for i, e in enumerate(edu):
            p = f"education[{i}]"
            if not isinstance(e, dict):
                _err(errors, p, "expected an object")
                continue
            for field in ("qualification", "institution", "dates"):
                _check_str(e.get(field), f"{p}.{field}", errors)

    langs = data.get("languages")
    if not isinstance(langs, list):
        _err(errors, "languages", "expected a list")
    else:
        for i, l in enumerate(langs):
            p = f"languages[{i}]"
            if not isinstance(l, dict):
                _err(errors, p, "expected an object")
                continue
            for field in ("language", "level"):
                _check_str(l.get(field), f"{p}.{field}", errors)

    return errors


def cv_json_to_markdown(data):
    """Deterministic Markdown projection so the existing viewer/download/approve
    paths keep working unchanged. Phase 3b's template reads content_json instead."""
    lines = ["## Profile", "", data["profile"].strip(), "", "## What I lead", ""]
    lines += [f"- **{item['label']}:** {item['detail']}" for item in data["what_i_lead"]]
    lines += ["", "## Experience", ""]
    for e in data["experience"]:
        lines.append(f"**{e['role']} — {e['company']}**")
        logo_key = e.get("logo_key")
        parts = [e.get("dates") or "", e.get("context") or ""]
        if logo_key:
            parts.append(f"logo: {logo_key}")
        meta = " · ".join(x.strip() for x in parts if x and x.strip())
        if meta:
            lines.append(f"*{meta}*")
        lines.append("")
        lines += [f"- {b}" for b in e["bullets"]]
        lines.append("")
    projects = data.get("projects") or []
    if projects:
        lines += ["## Projects", ""]
        for pr in projects:
            lines.append(f"**{pr['name']}**")
            if pr.get("context"):
                lines.append(f"*{pr['context']}*")
            lines.append("")
            lines += [f"- {b}" for b in pr["bullets"]]
            lines.append("")
    lines += ["## Education", ""]
    for ed in data["education"]:
        lines.append(f"- {ed['qualification']}, {ed['institution']} ({ed['dates']})")
    lines += ["", "## Languages", ""]
    for l in data["languages"]:
        lines.append(f"- {l['language']} — {l['level']}")
    return "\n".join(lines).strip() + "\n"
