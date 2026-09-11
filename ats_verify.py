#!/usr/bin/env python3
"""
ATS text-layer verification (Phase 4 prep).
============================================
A CV that looks perfect on screen can still fail an ATS if the PDF's text
layer doesn't carry what the eye sees — icon-only contact glyphs, headshots
and logos eating reading order, or a font substitution that turns characters
into (cid:N) / U+FFFD garbage. This module checks the actual extracted text
an ATS parser would see, via poppler's pdftotext, not just the rendered
pixels.

Deliberately NOT using pypdf for extraction: it was found (Phase 3c/cockpit-
usable work) to silently drop entire text runs against this project's
embedded variable Inter font. pdftotext (poppler) has been reliable
throughout this build. See tests/test_pdf_export.py for the same finding.

Each check_* function returns a list of problem strings — empty list means
pass. Built as a standalone checker plus a retroactive report first, before
wiring it into the download path — a bad text layer might already be
sitting in applications already sent, and that needs a look before the
gate starts blocking new ones. It's since been wired in: app.py's download
route runs this and blocks the download on any hit (see verify_ats_text_layer's
callers).
"""

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

REPLACEMENT_CHAR = "�"
CID_RE = re.compile(r"\(cid:\d+\)")


class PdftotextNotFound(RuntimeError):
    def __init__(self):
        super().__init__(
            "poppler's pdftotext is not installed. Install it with:\n"
            "  macOS:         brew install poppler\n"
            "  Debian/Ubuntu: apt-get install poppler-utils\n"
        )


def _require_pdftotext():
    if shutil.which("pdftotext") is None:
        raise PdftotextNotFound()


PDFTOTEXT_TIMEOUT_S = 30


def extract_text(pdf_path, page=None):
    """Full-document (or single-page) text via `pdftotext -layout` — the
    same view of the PDF an ATS text-parser gets, not what the eye sees."""
    _require_pdftotext()
    cmd = ["pdftotext", "-layout"]
    if page is not None:
        cmd += ["-f", str(page), "-l", str(page)]
    cmd += [str(pdf_path), "-"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True, timeout=PDFTOTEXT_TIMEOUT_S
    )
    return result.stdout


def page_count(pdf_path):
    _require_pdftotext()  # pdfinfo ships alongside pdftotext in poppler
    result = subprocess.run(
        ["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=True,
        timeout=PDFTOTEXT_TIMEOUT_S,
    )
    m = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    return int(m.group(1)) if m else None


def check_no_cid_or_replacement_chars(text):
    """Font substitution / broken cmap shows up as (cid:N) markers or the
    U+FFFD replacement character in extracted text — both mean an ATS parser
    sees garbage instead of the character that's visually rendered."""
    problems = []
    cids = CID_RE.findall(text)
    if cids:
        problems.append(f"{len(cids)} (cid:N) marker(s) in extracted text, e.g. {cids[0]}")
    n_repl = text.count(REPLACEMENT_CHAR)
    if n_repl:
        problems.append(f"{n_repl} replacement character(s) (U+FFFD) in extracted text")
    return problems


def check_contact_is_literal_text(text, email=None, phone=None):
    """Contact details must appear as literal text, not only carried by an
    icon glyph or a hyperlink annotation an ATS text-parser never sees.
    Checking presence in the extracted text is the whole test — if it's not
    here, a text-parser doesn't have it, regardless of how it looks on
    screen or whether it's also a clickable link."""
    problems = []
    if email and email not in text:
        problems.append(f"email '{email}' not found as literal text in extraction")
    if phone and phone not in text:
        problems.append(f"phone '{phone}' not found as literal text in extraction")
    return problems


def check_reading_order(text, landmarks):
    """Section headings must appear in the extracted text in the same order
    they appear visually. Checks position-in-string is strictly increasing
    across whichever landmarks are actually present — a scrambled text
    layer (a real risk with multi-column CSS, absolutely positioned blocks,
    or float-based layouts) shows up as an out-of-order or missing landmark
    even though the page looks fine rendered."""
    problems = []
    last_pos, last_label = -1, None
    for label in landmarks:
        pos = text.find(label)
        if pos == -1:
            continue  # not every landmark applies to every document type
        if pos <= last_pos:
            problems.append(
                f"'{label}' appears at position {pos}, not after "
                f"'{last_label}' at {last_pos} — reading order looks scrambled"
            )
        last_pos, last_label = pos, label
    return problems


def check_no_internal_repetition(text, min_len=40):
    """A duplicated sentence/paragraph in the extracted text is a real
    signal of accidental content duplication (a rendering bug, not
    deliberate keyword stuffing in this codebase — but it would look
    exactly like stuffing to an ATS, so it's checked the same way)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) >= min_len]
    counts = Counter(sentences)
    dupes = {s: c for s, c in counts.items() if c > 1}
    if not dupes:
        return []
    return [f"repeated {c}x: {s[:80]}{'…' if len(s) > 80 else ''}" for s, c in dupes.items()]


# Common function words excluded from the JD keyword candidate set — without
# this, a live test blocked a real download over "that" (7x in a CV vs 2x in
# its JD): ordinary grammar, not a posting keyword, and certainly not
# stuffing. Deliberately not reusing verifier.py's _STOPWORDS: that list is
# tuned to exclude connective words from proper-noun PHRASE extraction, a
# different job with different edge cases, so a shared list would couple two
# things that should be free to diverge.
_KEYWORD_STOPWORDS = {
    "that", "this", "these", "those", "with", "have", "has", "had", "will",
    "would", "could", "should", "your", "you", "our", "their", "them", "they",
    "from", "into", "than", "then", "when", "what", "where", "which", "while",
    "about", "across", "after", "before", "between", "through", "under",
    "over", "each", "every", "some", "such", "very", "also", "more", "most",
    "other", "including", "include", "includes", "please", "must", "able",
    "role", "roles", "job", "jobs", "work", "works", "working", "team",
    "teams", "years", "year", "experience", "strong", "excellent", "looking",
    "ideal", "candidate", "candidates", "apply", "application", "applicants",
    "company", "companies", "position", "opportunity", "responsibilities",
    "requirements", "preferred", "required", "based", "within", "along",
    "well", "here", "there", "been", "being", "were", "does", "doing", "done",
}


def check_posting_keywords(text, jd_text, stuffing_ratio=3, stuffing_min_count=6):
    """Compares keyword frequency in the CV's extracted text against the job
    posting: present or honestly absent is fine either way, stuffed is not.
    'Stuffed' = a keyword appears far more often in the CV than the JD's own
    usage would justify (ratio AND absolute-count gated, so a JD that says
    'design' once doesn't flag a CV that says it four times naturally).

    Returns None (not a list) when jd_text isn't available — 'not
    applicable' is itself an honest answer, not a silent pass, and callers
    must not treat None as 'no problems found'.
    """
    if not jd_text:
        return None
    jd_words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", jd_text.lower())
    jd_counts = Counter(w for w in jd_words if w not in _KEYWORD_STOPWORDS)
    keywords = [w for w, c in jd_counts.items() if c >= 2]  # JD's own emphasis, not every word
    text_words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text.lower())
    text_counts = Counter(text_words)

    present, absent, stuffed = [], [], []
    for kw in keywords:
        cv_count = text_counts.get(kw, 0)
        jd_count = jd_counts[kw]
        if cv_count == 0:
            absent.append(kw)
        elif cv_count >= stuffing_min_count and cv_count > jd_count * stuffing_ratio:
            stuffed.append((kw, cv_count, jd_count))
        else:
            present.append(kw)
    return {"present": present, "honestly_absent": absent, "stuffed": stuffed}


# Section landmarks for this project's current CV template (templates/cv.html),
# in the order the template emits them. Older/external PDFs use their own
# headings (checked ad hoc in the retroactive report, not via this constant).
CV_LANDMARKS = ["PROFILE", "WHAT I LEAD", "EXPERIENCE", "PROJECTS", "EDUCATION", "LANGUAGES"]


def verify_ats_text_layer(pdf_path, email=None, phone=None, landmarks=None, jd_text=None):
    """Run the full battery and return {check_name: [problems]} plus
    'posting_keywords': dict-or-None. Empty problem lists mean pass."""
    text = extract_text(pdf_path)
    landmarks = landmarks if landmarks is not None else CV_LANDMARKS
    return {
        "cid_or_replacement_chars": check_no_cid_or_replacement_chars(text),
        "contact_literal_text": check_contact_is_literal_text(text, email, phone),
        "reading_order": check_reading_order(text, landmarks),
        "internal_repetition": check_no_internal_repetition(text),
        "posting_keywords": check_posting_keywords(text, jd_text),
        "page_count": page_count(pdf_path),
        "extracted_chars": len(text),
    }
