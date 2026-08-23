#!/usr/bin/env python3
"""
Job scanner — provider handlers, title/location filtering, scan orchestration.
================================================================================
No standalone entry point here — this module is imported, not run directly.
app.py's scan_into_db() (the UI's "Scan now" and cron_scan.py's headless
scan both call it) drives scan_all_companies_detailed() and writes results
to cockpit.db, recording scan_runs/scan_run_companies as it goes.

Targeting (which companies, which title/location patterns count as a
match) is config-driven — see load_scanner_config() and config.yaml's
scanner: block, not anything hardcoded here.
"""

import requests
import re
import time
import yaml
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# ============================================================
# CONFIGURATION — companies, title patterns, and location patterns are
# personal targeting, not code, and live in config.yaml's `scanner:`
# block (gitignored — your real search, not tracked). See
# load_scanner_config() below. config.yaml.example ships a small,
# generic starter set (a handful of well-known Greenhouse/Lever/Ashby
# companies, broad title patterns, a permissive location filter) so a
# first scan against the example returns something rather than zero,
# without shipping anyone's actual job search as the default.
# ============================================================

# Server-side keyword terms for Workday tenants. Big tenants (Roche, Novartis)
# host thousands of postings — keyword search keeps each query under the
# pagination cap. Results are merged and deduped, then title_patterns filters.
# Generic enough (not personal targeting) to stay a code constant.
WORKDAY_SEARCH_TERMS = ["design", "user experience"]

SCANNER_CONFIG_PATH = Path(__file__).parent / "config.yaml"


class ScannerConfigError(Exception):
    """config.yaml's scanner: block is missing or malformed. Raised with a
    readable message, never a raw traceback — the loader's whole job is to
    fail clearly here so a stranger's first run tells them exactly what to
    fix in their config instead of dying inside a regex or a KeyError."""


def load_scanner_config(path=None):
    """Read companies/title_patterns/location_patterns from config.yaml's
    scanner: block. Returns (companies, title_patterns, location_patterns) —
    companies as a list of (company, handler, target) tuples, matching the
    shape every search_* provider function already expects; the two
    pattern lists as plain regex-string lists, matching is_title_match()/
    location_ok()'s existing contract."""
    p = Path(path) if path else SCANNER_CONFIG_PATH
    if not p.exists():
        raise ScannerConfigError(
            f"{p} not found. Copy config.yaml.example to config.yaml and fill "
            "in your own scanner targets (or keep the example's generic "
            "starter set) before running the scanner."
        )
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    scanner_cfg = cfg.get("scanner")
    if not isinstance(scanner_cfg, dict):
        raise ScannerConfigError(
            f"{p} has no top-level 'scanner:' block — see config.yaml.example "
            "for the expected shape."
        )

    raw_companies = scanner_cfg.get("companies")
    if not isinstance(raw_companies, list) or not raw_companies:
        raise ScannerConfigError(
            "scanner.companies must be a non-empty list — see config.yaml.example."
        )
    companies = []
    for i, entry in enumerate(raw_companies):
        if not isinstance(entry, dict):
            raise ScannerConfigError(
                f"scanner.companies[{i}] must be a mapping with company/handler/"
                f"target keys, got {type(entry).__name__}."
            )
        missing = [k for k in ("company", "handler", "target") if k not in entry]
        if missing:
            raise ScannerConfigError(
                f"scanner.companies[{i}] is missing required key(s): {', '.join(missing)}."
            )
        companies.append((entry["company"], entry["handler"], entry["target"]))

    title_patterns = _load_pattern_list(scanner_cfg, "title_patterns")
    location_patterns = _load_pattern_list(scanner_cfg, "location_patterns")

    return companies, title_patterns, location_patterns


def _load_pattern_list(scanner_cfg, key):
    patterns = scanner_cfg.get(key)
    if not isinstance(patterns, list) or not patterns:
        raise ScannerConfigError(f"scanner.{key} must be a non-empty list of regex strings.")
    for i, pat in enumerate(patterns):
        if not isinstance(pat, str):
            raise ScannerConfigError(f"scanner.{key}[{i}] must be a string, got {type(pat).__name__}.")
        try:
            re.compile(pat)
        except re.error as e:
            raise ScannerConfigError(f"scanner.{key}[{i}] is not a valid regex ({pat!r}): {e}")
    return patterns


def load_manual_check(path=None):
    """Read scanner.manual_check from config.yaml — the informational list
    of companies with no public ATS API, checked by hand instead of
    scanned. Purely a display list: nothing in this module parses or acts
    on it, only the sourcing skills that read config.yaml directly.

    Optional and permissive by design, unlike load_scanner_config()'s
    companies/title_patterns/location_patterns: a missing key or missing
    file returns [] rather than raising, since a stranger's first config
    may not have any known no-API companies yet, and there's nothing
    broken about that — see config.yaml.example."""
    p = Path(path) if path else SCANNER_CONFIG_PATH
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    scanner_cfg = cfg.get("scanner")
    if not isinstance(scanner_cfg, dict):
        return []
    manual_check = scanner_cfg.get("manual_check")
    if manual_check is None:
        return []
    if not isinstance(manual_check, list) or not all(isinstance(x, str) for x in manual_check):
        raise ScannerConfigError("scanner.manual_check must be a list of strings if present.")
    return manual_check

# Title-based track classifier. Order matters: advisory is checked first so
# "Principal UX Consultant" lands in Advisory, not Senior IC.
CATEGORY_RULES = [
    ("Advisory", [
        r"(?i)\bconsult", r"(?i)advisor|advisory",
        r"(?i)(design|ux|experience)\s*strateg",
    ]),
    ("Leadership", [
        r"(?i)head\s*of", r"(?i)director", r"(?i)vp|vice\s*president",
        r"(?i)chief\s*design\s*officer", r"(?i)(design|ux)\s*manager",
    ]),
    ("Senior IC", [
        r"(?i)(senior|sr\.?|lead|staff|principal).*designer",
        r"(?i)(design|ux)\s*lead",
    ]),
]


def categorize_title(title):
    """Map a matched title to a search track: Leadership / Senior IC /
    Advisory. Returns 'Review' when nothing fits (triage by hand)."""
    for label, patterns in CATEGORY_RULES:
        if any(re.search(p, title) for p in patterns):
            return label
    return "Review"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

# ============================================================
# PORTAL HANDLERS
# ============================================================

def search_workday(company_name, api_url):
    """POST keyword searches (one per WORKDAY_SEARCH_TERMS), merged + deduped.

    The original version hardcoded searchText="Director Product Management",
    which server-side filtered every Workday tenant for PM roles regardless of
    TITLE_PATTERNS. Now each term in WORKDAY_SEARCH_TERMS runs as its own
    search. Returns every match the search terms turned up, unfiltered by
    title — TITLE_PATTERNS is applied centrally in
    scan_all_companies_detailed(), not here, so every dropped title is
    visible for scan observability instead of silently discarded.
    """
    seen = {}
    max_total = 0
    try:
        for term in WORKDAY_SEARCH_TERMS:
            payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}
            resp = SESSION.post(api_url, json=payload, timeout=15)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"

            data = resp.json()
            total = data.get("total", 0)
            max_total = max(max_total, total)
            jobs = data.get("jobPostings", [])

            while len(jobs) < total and len(jobs) < 1200:
                payload["offset"] = len(jobs)
                resp = SESSION.post(api_url, json=payload, timeout=15)
                if resp.status_code != 200:
                    break
                new_jobs = resp.json().get("jobPostings", [])
                if not new_jobs:
                    break
                jobs.extend(new_jobs)

            for job in jobs:
                title = job.get("title", "")
                ext_path = job.get("externalPath", "")
                jid = ext_path.split("/")[-1] if ext_path else ""
                if not jid or jid in seen:
                    continue
                parts = api_url.split("/wday/cxs/")
                if len(parts) == 2:
                    domain = parts[0]
                    site = parts[1].split("/jobs")[0].split("/")[-1]
                    # ext_path already starts with "/job/..." (Workday's own
                    # externalPath) — do not prepend another "job" segment.
                    job_url = f"{domain}/en-US/{site}{ext_path}"
                else:
                    job_url = ext_path
                seen[jid] = {
                    "company": company_name,
                    "title": title,
                    "location": job.get("locationsText", ""),
                    "url": job_url,
                    "posted": job.get("postedOn", ""),
                    "portal": "workday",
                    "job_id": jid,
                }
            time.sleep(0.2)

        return list(seen.values()), f"~{max_total} searched, {len(seen)} matched"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]


def search_greenhouse(company_name, api_url):
    try:
        resp = SESSION.get(api_url, params={"content": "true"}, timeout=15)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        jobs = resp.json().get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            matches.append({
                "company": company_name,
                "title": title,
                "location": job.get("location", {}).get("name", ""),
                "url": job.get("absolute_url", ""),
                "posted": (job.get("updated_at") or "")[:10],
                "portal": "greenhouse",
                "job_id": str(job.get("id", "")),
            })

        return matches, f"{len(jobs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]


def search_lever(company_name, api_url):
    try:
        resp = SESSION.get(api_url, timeout=15)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        jobs = resp.json()
        if not isinstance(jobs, list):
            return None, "Unexpected response format"

        matches = []
        for job in jobs:
            title = job.get("text", "")
            created = job.get("createdAt", 0)
            posted = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") if created else ""
            matches.append({
                "company": company_name,
                "title": title,
                "location": job.get("categories", {}).get("location", ""),
                "url": job.get("hostedUrl", ""),
                "posted": posted,
                "portal": "lever",
                "job_id": str(job.get("id", "")),
            })

        return matches, f"{len(jobs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]


def search_ashby(company_name, board_slug):
    """Search Ashby job board API."""
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{board_slug}"
    try:
        resp = SESSION.get(api_url, timeout=15)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        data = resp.json()
        jobs = data.get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            loc = job.get("location", "")
            job_url = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{board_slug}/{job.get('id', '')}"
            matches.append({
                "company": company_name,
                "title": title,
                "location": loc if isinstance(loc, str) else "",
                "url": job_url,
                "posted": (job.get("publishedAt") or "")[:10],
                "portal": "ashby",
                "job_id": str(job.get("id", "")),
            })

        return matches, f"{len(jobs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]


def search_smartrecruiters(company_name, company_id):
    """Search SmartRecruiters public API."""
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    try:
        all_jobs = []
        offset = 0
        while True:
            resp = SESSION.get(api_url, params={"offset": offset, "limit": 100}, timeout=15)
            if resp.status_code != 200:
                if not all_jobs:
                    return None, f"HTTP {resp.status_code}"
                break
            data = resp.json()
            content = data.get("content", [])
            if not content:
                break
            all_jobs.extend(content)
            offset += len(content)
            if offset >= data.get("totalFound", 0):
                break

        matches = []
        for job in all_jobs:
            title = job.get("name", "")
            loc = job.get("location", {})
            loc_str = f"{loc.get('city', '')}, {loc.get('region', '')}".strip(", ")
            job_url = job.get("ref", "") or job.get("company", {}).get("identifier", "")
            matches.append({
                "company": company_name,
                "title": title,
                "location": loc_str,
                "url": job_url,
                "posted": (job.get("releasedDate") or "")[:10],
                "portal": "smartrecruiters",
                "job_id": str(job.get("id", "")),
            })

        return matches, f"{len(all_jobs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]


def search_workable(company_name, account_slug):
    """Search Workable's widget API (Swiss startups love Workable).

    Not an officially documented public API, but stable in practice. If it
    breaks, move the company to MANUAL_CHECK.
    """
    api_url = f"https://apply.workable.com/api/v1/widget/accounts/{account_slug}?details=false"
    try:
        resp = SESSION.get(api_url, timeout=15)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        data = resp.json()
        jobs = data.get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            city = job.get("city") or ""
            country = job.get("country") or ""
            location = ", ".join(x for x in (city, country) if x)
            jid = job.get("shortcode") or str(job.get("id", ""))
            matches.append({
                "company": company_name,
                "title": title,
                "location": location,
                "url": job.get("url") or job.get("shortlink") or f"https://apply.workable.com/{account_slug}/",
                "posted": (job.get("published_on") or "")[:10],
                "portal": "workable",
                "job_id": f"{account_slug}_{jid}",
            })

        return matches, f"{len(jobs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]
    except ValueError as e:
        return None, f"Bad JSON: {str(e)[:80]}"


_ROBOTS_CACHE = {}


def _robots_allows(url, user_agent="*"):
    """Respected at request time, not just during the one-off investigation
    that vetted these platforms — Oracle HCM and Jibe are new enough here
    that the check belongs in the code, not just in HANDOFF.md. Caches one
    RobotFileParser per domain per run. A fetch failure (no robots.txt at
    all, network error) is NOT the same as an explicit Disallow — every
    other provider here was vetted by hand against a domain that often has
    no robots.txt file whatsoever (e.g. Oracle's own iaadtu.fa.ocs.oraclecloud.eu
    tenant: 404), so this fails open rather than blocking on absence."""
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain not in _ROBOTS_CACHE:
        rp = RobotFileParser()
        try:
            resp = SESSION.get(f"{domain}/robots.txt", timeout=10)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.allow_all = True
        except requests.exceptions.RequestException:
            rp.allow_all = True
        _ROBOTS_CACHE[domain] = rp
    return _ROBOTS_CACHE[domain].can_fetch(user_agent, url)


def search_oracle_hcm(company_name, base_url, site_number):
    """Oracle Recruiting Cloud's public Candidate Experience REST API —
    documented, unauthenticated, no session/CSRF tokens (unlike SAP
    SuccessFactors' career-site search, which is a session-bound DWR RPC
    call and stays MANUAL_CHECK for exactly that reason — see HANDOFF.md).

    base_url is the Oracle Cloud tenant root, e.g.
    "https://iaadtu.fa.ocs.oraclecloud.eu". site_number is the
    CandidateExperience site id (e.g. "CX_1"), visible in the site's own
    /hcmUI/CandidateExperience/en/sites/<site_number> URL.

    Pattern generalises across any Oracle Recruiting Cloud tenant (common
    in European banking/insurance), so this one handler is meant to serve
    every future company on the platform, not just the one it launched
    with."""
    api_url = f"{base_url}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    if not _robots_allows(api_url):
        return None, "disallowed by robots.txt"

    try:
        all_reqs = []
        offset = 0
        limit = 25
        total = None
        while total is None or offset < total:
            params = {
                "onlyData": "true",
                "expand": "requisitionList",
                "finder": f"findReqs;siteNumber={site_number},limit={limit},offset={offset}",
            }
            resp = SESSION.get(api_url, params=params, timeout=15)
            if resp.status_code != 200:
                if not all_reqs:
                    return None, f"HTTP {resp.status_code}"
                break
            block = resp.json().get("items", [{}])[0]
            total = block.get("TotalJobsCount", 0)
            reqs = block.get("requisitionList", [])
            if not reqs:
                break
            all_reqs.extend(reqs)
            offset += len(reqs)
            time.sleep(0.2)

        matches = []
        for req in all_reqs:
            title = req.get("Title", "")
            rid = req.get("Id", "")
            matches.append({
                "company": company_name,
                "title": title,
                "location": req.get("PrimaryLocation", "") or "",
                "url": f"{base_url}/hcmUI/CandidateExperience/en/sites/{site_number}/job/{rid}",
                "posted": req.get("PostedDate", "") or "",
                "portal": "oracle_hcm",
                "job_id": f"{site_number}_{rid}",
            })

        return matches, f"{len(all_reqs)} total"
    except requests.exceptions.RequestException as e:
        return None, str(e)[:100]
    except ValueError as e:
        return None, f"Bad JSON: {str(e)[:80]}"


# ============================================================
# TITLE + LOCATION MATCHING
# ============================================================

def is_title_match(title, patterns):
    """patterns: the caller's title_patterns list — from config.yaml's
    scanner.title_patterns (load_scanner_config()), or an explicit list a
    caller (a test, a different entry point) passes directly. No hidden
    module-level default — every caller states its own patterns."""
    for pattern in patterns:
        if re.search(pattern, title):
            return True
    return False


def location_ok(location, patterns):
    """True if the location matches one of `patterns`. Empty locations pass
    (can't judge — keep and triage by hand). patterns: the caller's
    location_patterns list, same contract as is_title_match()."""
    if not location or not location.strip():
        return True
    return any(re.search(p, location) for p in patterns)


# ============================================================
# COMPANY DEFINITIONS — moved to config.yaml's scanner.companies and
# scanner.manual_check (2026-08-19 / 2026-08-20). Real targets are
# personal to whoever's search this is and live in the gitignored
# config.yaml, not here — load_manual_check() above reads the latter.
# The full history of how each entry was verified — portal gotchas, which
# providers turned out to have live Oracle HCM APIs vs. stay manual-check,
# and why — is preserved in HANDOFF.md and this file's own git history,
# not duplicated in config.yaml's comments.
# ============================================================


# Exact message _robots_allows-blocked providers return — matched by
# scan_all_companies_detailed() to classify outcome='skipped_robots'
# instead of 'error'. A named constant instead of a literal string
# repeated at both call sites, so a future rewording can't silently break
# the classification.
ROBOTS_SKIP_MESSAGE = "disallowed by robots.txt"


def scan_all_companies_detailed(companies=None, title_patterns=None, location_patterns=None):
    """Run all API scans with full per-company funnel detail: raw postings
    fetched, which titles matched title_patterns (and which didn't — the
    dropped ones, capped at 50), which of those matched location_patterns
    (and which didn't, also capped), and the outcome/error for the company
    itself. Title and location filtering both happen HERE, centrally —
    providers only fetch and normalize, they no longer filter — so every
    dropped posting is visible for scan observability instead of quietly
    disappearing inside eight different provider functions.

    companies/title_patterns/location_patterns default to
    load_scanner_config()'s result (config.yaml's scanner: block) when not
    passed explicitly — real callers (app.py's scan_into_db(), driven by
    the UI's "Scan now" and by cron_scan.py) rely on that default; tests
    pass their own fixture values instead so unit tests never depend on a
    real config.yaml existing on disk.

    Returns a list of dicts:
        {company, portal, outcome, error_detail, raw_count,
         title_matches, dropped_titles, location_matches, dropped_locations}
    title_matches/location_matches are full match-dict lists; dropped_titles/
    dropped_locations are the actual dropped values (capped at 50 each) —
    that's the field that actually answers "why did nothing land," not
    just a count.

    scan_all_companies() below is a thin backward-compat wrapper around
    this for a caller that only needs the flat (matches, errors) shape."""
    if companies is None or title_patterns is None or location_patterns is None:
        loaded_companies, loaded_title_patterns, loaded_location_patterns = load_scanner_config()
        companies = loaded_companies if companies is None else companies
        title_patterns = loaded_title_patterns if title_patterns is None else title_patterns
        location_patterns = loaded_location_patterns if location_patterns is None else location_patterns

    results = []

    for company, handler, url_or_slug in companies:
        if handler == "workday":
            raw, msg = search_workday(company, url_or_slug)
        elif handler == "greenhouse":
            raw, msg = search_greenhouse(company, url_or_slug)
        elif handler == "lever":
            raw, msg = search_lever(company, url_or_slug)
        elif handler == "ashby":
            raw, msg = search_ashby(company, url_or_slug)
        elif handler == "smartrecruiters":
            raw, msg = search_smartrecruiters(company, url_or_slug)
        elif handler == "workable":
            raw, msg = search_workable(company, url_or_slug)
        elif handler == "oracle_hcm":
            base_url, site_number = url_or_slug.split("|", 1)
            raw, msg = search_oracle_hcm(company, base_url, site_number)
        else:
            raw, msg = None, f"Unknown handler: {handler}"

        if raw is None:
            outcome = "skipped_robots" if msg == ROBOTS_SKIP_MESSAGE else "error"
            results.append({
                "company": company, "portal": handler, "outcome": outcome,
                "error_detail": msg, "raw_count": 0,
                "title_matches": [], "dropped_titles": [],
                "location_matches": [], "dropped_locations": [],
            })
            print(f"  ✗ {company:25s} | {msg}")
        else:
            title_matches = [m for m in raw if is_title_match(m["title"], title_patterns)]
            dropped_titles = [m["title"] for m in raw if not is_title_match(m["title"], title_patterns)]
            location_matches = [m for m in title_matches if location_ok(m.get("location", ""), location_patterns)]
            dropped_locations = [m.get("location", "") for m in title_matches
                                  if not location_ok(m.get("location", ""), location_patterns)]
            results.append({
                "company": company, "portal": handler, "outcome": "ok",
                "error_detail": None, "raw_count": len(raw),
                "title_matches": title_matches, "dropped_titles": dropped_titles[:50],
                "location_matches": location_matches, "dropped_locations": dropped_locations[:50],
            })
            print(f"  ✓ {company:25s} | {msg} | "
                  f"{len(title_matches)} title-matched, {len(location_matches)} location-matched")

        time.sleep(0.3)

    return results


def scan_all_companies(companies=None, title_patterns=None, location_patterns=None):
    """Back-compat wrapper: flat (matches, errors) shape, title-filtered
    but NOT location-filtered — exactly what this returned before scan
    observability split filtering out into scan_all_companies_detailed().
    For a caller that doesn't need the full funnel detail. Same
    optional-params-default-to-config.yaml contract as
    scan_all_companies_detailed()."""
    detailed = scan_all_companies_detailed(companies, title_patterns, location_patterns)
    all_matches = []
    errors = []
    for c in detailed:
        if c["outcome"] == "ok":
            all_matches.extend(c["title_matches"])
        else:
            errors.append((c["company"], c["portal"], c["error_detail"]))
    return all_matches, errors


