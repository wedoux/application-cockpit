#!/usr/bin/env python3
"""
JD fetch fallback chain (Phase 2, spec section 8).
==================================================
Turns a job URL into JD text without leaving the app. No step is a dead end:

  1. Workday CXS JSON API, if the host is a myworkdayjobs.com tenant. Same
     public-API pattern job_scanner.py already uses for listing search;
     this hits the per-job detail endpoint instead. Deterministic, no
     browser needed.
  2. Plain HTTP GET + readability extraction (requests + trafilatura).
  3. If the page is a JS shell / near-empty AND Layer 2 is enabled, call the
     read-only browser worker. That worker is a Phase 5 build — here it is a
     stub that reports "unavailable", so the chain falls through.
  4. Still nothing -> the caller shows a paste box.

Read-only throughout. This never fills or submits anything.
"""

import re
from urllib.parse import urlparse

import requests
import trafilatura

SUBSTANTIAL = 800            # chars of extracted text that count as a real JD
LAYER2_ENABLED = False       # browser worker (Phase 5); stub below

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,fr;q=0.8,de;q=0.6",
}


def _http_get(url, timeout=15):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _extract(html):
    """Return (text, title) from raw HTML using trafilatura."""
    text = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_recall=True,
    ) or ""
    title = None
    try:
        md = trafilatura.extract_metadata(html)
        if md and getattr(md, "title", None):
            title = md.title.strip() or None
    except Exception:  # noqa: BLE001 — metadata is best-effort
        pass
    return text.strip(), title


def _browser_worker(url):
    """Layer 2 stub. The real read-only browser extractor is Phase 5.
    Returns None so the chain degrades to paste."""
    return None


def _workday_ext_path(path_parts):
    """Given URL path segments after /<locale>/<site>/, return Workday's real
    externalPath (starts with /job/...).

    job_scanner.py builds the browser-facing URL as
    f"{domain}/en-US/{site}/job{ext_path}", and ext_path itself already
    starts with "/job/...", so stored URLs carry a doubled "job/job"
    segment. Collapse that back to the single externalPath Workday's own
    API expects.
    """
    remainder = list(path_parts)
    if len(remainder) >= 2 and remainder[0] == "job" and remainder[1] == "job":
        remainder = remainder[1:]
    elif not remainder or remainder[0] != "job":
        remainder = ["job"] + remainder
    return "/" + "/".join(remainder)


def _workday_provider(url):
    """Fetch JD text via Workday's CXS JSON API — the same public-API
    pattern job_scanner.py uses for listing search, here hitting the
    per-job detail endpoint. Returns (text, title) or None if this isn't a
    Workday URL, or the API call fails for any reason."""
    parsed = urlparse(url)
    if "myworkdayjobs.com" not in parsed.netloc:
        return None
    tenant = parsed.netloc.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3:
        return None
    site = parts[1]
    ext_path = _workday_ext_path(parts[2:])
    cxs_url = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}{ext_path}"

    try:
        resp = requests.get(cxs_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    info = data.get("jobPostingInfo") or {}
    html = (info.get("jobDescription") or "").strip()
    if not html:
        return None

    text, _ = _extract(f"<html><body>{html}</body></html>")
    if not text:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
    return text, info.get("title")


_TITLE_STOPWORDS = {
    "of", "the", "a", "an", "and", "or", "for", "in", "at", "on", "to", "with", "&",
}


def title_words_present(title, text):
    """Second signal alongside the char threshold (SUBSTANTIAL): does the
    role's own title actually show up in the extracted text? Danske Bank
    (a real case) cleared 800 chars on generic legal boilerplate with zero
    of its title's words present — a char count alone can't catch that
    class of false positive, only content relevance can.

    Strips parenthetical asides (req numbers, location tags) from the
    title, keeps words >2 chars that aren't generic stopwords, and checks
    how many appear (word-bounded, case-insensitive) in text. Returns
    {"significant_words", "matched", "missing", "hits", "total"}."""
    stripped = re.sub(r"\([^)]*\)", " ", title or "")
    words = re.findall(r"[A-Za-zÀ-ÿ]+", stripped)
    significant = [w for w in words if len(w) > 2 and w.lower() not in _TITLE_STOPWORDS]

    text_lower = (text or "").lower()
    matched = [w for w in significant if re.search(rf"\b{re.escape(w.lower())}\b", text_lower)]
    missing = [w for w in significant if w not in matched]
    return {
        "significant_words": significant,
        "matched": matched,
        "missing": missing,
        "hits": len(matched),
        "total": len(significant),
    }


def fetch_jd(url):
    """Return (text, title, source). source in fetch|browser|none.
    Never raises on network/parse failure — degrades to ('', title?, 'none')."""
    url = (url or "").strip()
    if not url:
        return "", None, "none"

    title = None

    # 1. Workday CXS JSON API, if this is a myworkdayjobs.com URL
    got = _workday_provider(url)
    if got:
        text, wd_title = got
        if len(text) >= SUBSTANTIAL:
            return text, wd_title, "fetch"

    # 2. Plain HTTP + readability
    try:
        html = _http_get(url)
        text, title = _extract(html)
        if len(text) >= SUBSTANTIAL:
            return text, title, "fetch"
    except requests.RequestException:
        pass

    # 3. Browser worker (Layer 2), if wired up
    if LAYER2_ENABLED:
        got = _browser_worker(url)
        if got and len((got.get("text") or "")) >= SUBSTANTIAL:
            return got["text"].strip(), got.get("title") or title, "browser"

    # 4. Dead end -> caller shows a paste box
    return "", title, "none"


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    text, title, source = fetch_jd(u)
    print(f"source={source} title={title!r} chars={len(text)}")
    print("---")
    print(text[:800])
