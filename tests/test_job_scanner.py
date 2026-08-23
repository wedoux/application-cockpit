import job_scanner


# TITLE_PATTERNS widened 2026-08-19 against real dropped_titles evidence
# from scan observability (Logitech, Nexthink, DEPT) — three English
# word-order shapes, not a language gap. Locks in the exact tuning: an
# earlier, looser version of the "design as a bare noun" shape also caught
# hardware/chip design engineering roles and retail "Experience Manager"
# titles before being tightened against this same evidence.
#
# Test-local fixture, not read from config.yaml — title_patterns moved to
# config in the scanner-config refactor (2026-08-19), and these tests need
# to lock in this exact tuning independent of whatever any given user's
# config.yaml happens to contain.
TITLE_PATTERNS = [
    r"(?i)head\s*of\s*(product\s*)?design",
    r"(?i)head\s*of\s*(ux|user\s*experience)",
    r"(?i)(design|ux|user\s*experience)\s*director",
    r"(?i)director\s*(of\s*|,\s*)?(product\s*|ux\s*|experience\s*)?design",
    r"(?i)director\s*(of\s*|,\s*)?(ux|user\s*experience)",
    r"(?i)(vp|vice\s*president)\W*(of\s*)?(product\s*)?design",
    r"(?i)chief\s*design\s*officer",
    r"(?i)head\s*of\s*digital\s*(product|experience)",
    r"(?i)(senior\s*)?(design|ux)\s*manager",
    r"(?i)(design|ux)\s*lead",
    r"(?i)(senior|sr\.?|lead|staff|principal)\b[\w/&.\- ]*\b(product|ux|user\s*experience|experience|interaction|service|platform|design\s*systems?)\b[\w/&.\- ]*\bdesigner",
    r"(?i)(senior|sr\.?|lead|staff|principal)\s+designer\b",
    r"(?i)(ux|user\s*experience|product|service|experience|design|digital\s*product)\s*consultant",
    r"(?i)consultant.*(ux|user\s*experience|\bdesign\b)",
    r"(?i)(design|ux|experience)\s*(strateg(ist|y)|advisor|advisory)",
    r"(?i)ai\s*(product\s*)?(ux\s*)?design",
    r"(?i)(head\s*of|director|principal|global\s*head\s*of)[\w/&,.\- ]{0,30}\b(visual\s*design|creative\s*&?\s*design|product\s*design|ux\s*design|design\s*operations)\b",
    r"(?i)\buser\s*experience\s*(manager|lead|director)\b",
    r"(?i)\b(manager|director|head|lead),\s*(product\s*)?(ux|user\s*experience)\s*design\b",
]

# Same rationale as TITLE_PATTERNS above: location_patterns also moved to
# config.yaml, so tests that need real location-filtering behaviour (e.g.
# "Geneva, Switzerland" matches, "Paris, France" doesn't) need their own
# fixture rather than reading any given user's config.
LOCATION_PATTERNS = [
    r"(?i)switzerland|suisse|schweiz|svizzera",
    r"(?i)gen[eè]ve|geneva",
    r"(?i)lausanne|vaud|nyon|morges|vevey|tolochenaz|aubonne|saint-prex|gland|prilly|cheseaux|saint-sulpice",
    r"(?i)\bbern\b|basel|b[aâ]le|z[uü]rich|\bzug\b|neuch[aâ]tel|le\s*brassus|la\s*chaux-de-fonds|grenchen",
    r"(?i)remote.*(europe|emea|\beu\b)|(europe|emea).*remote",
]


def test_title_match_widened_design_as_bare_noun_shape():
    assert job_scanner.is_title_match("Global Head of Creative & Design Operations", TITLE_PATTERNS)
    assert job_scanner.is_title_match("Associate Director, Visual Design", TITLE_PATTERNS)


def test_title_match_widened_user_experience_manager_shape():
    assert job_scanner.is_title_match("Senior End User Experience Manager", TITLE_PATTERNS)


def test_title_match_widened_comma_inverted_shape():
    assert job_scanner.is_title_match("Manager, Product UX Design (E-COMMERCE)", TITLE_PATTERNS)


def test_title_match_still_excludes_hardware_and_engineering_design_roles():
    """The bare-noun 'design' shape must not swallow hardware/chip/mechanical
    engineering titles that happen to contain the word "design" — these are
    real dropped titles from the same scan that a looser first draft of the
    widened pattern incorrectly matched."""
    assert not job_scanner.is_title_match("Principal IC Design Engineer - Analog", TITLE_PATTERNS)
    assert not job_scanner.is_title_match("Senior Principal Mechanical Design Engineer", TITLE_PATTERNS)
    assert not job_scanner.is_title_match(
        "Associate Director, Solution Design Expert (Advanced Therapies)", TITLE_PATTERNS)


def test_title_match_still_excludes_retail_experience_manager():
    """'Experience Manager' alone (no 'user') is usually retail/customer
    experience, not UX — a real dropped title from the same scan."""
    assert not job_scanner.is_title_match("Sales Experience Manager", TITLE_PATTERNS)


class _FakeWorkdaySearchResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def test_search_workday_does_not_double_the_job_segment(monkeypatch):
    """job_scanner's own URL builder used to write
    f"{domain}/en-US/{site}/job{ext_path}" where ext_path already starts
    with "/job/...", producing a doubled "job/job" segment in every stored
    Workday role URL. Regression: the built URL must contain exactly one
    "/job/" segment."""
    job = {
        "title": "Senior User Experience Designer",
        "externalPath": "/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142",
        "locationsText": "Lausanne, Switzerland",
        "postedOn": "Posted 5 Days Ago",
    }

    def fake_post(url, json=None, timeout=None):
        return _FakeWorkdaySearchResponse({"total": 1, "jobPostings": [job]})

    monkeypatch.setattr(job_scanner.SESSION, "post", fake_post)

    api_url = "https://logitech.wd5.myworkdayjobs.com/wday/cxs/logitech/Logitech/jobs"
    results, _summary = job_scanner.search_workday("Logitech", api_url)

    assert len(results) == 1
    url = results[0]["url"]
    assert url == ("https://logitech.wd5.myworkdayjobs.com/en-US/Logitech"
                    "/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142")
    assert url.count("/job/") == 1


# Oracle HCM (Oracle Recruiting Cloud) — public, unauthenticated
# recruitingCEJobRequisitions REST API. Unlike SAP SuccessFactors' DWR-based
# search (session/CSRF tokens, non-JSON wire format — stays MANUAL_CHECK,
# see HANDOFF.md), this is a clean JSON GET with pagination.

class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


def _reset_robots_cache(monkeypatch):
    monkeypatch.setattr(job_scanner, "_ROBOTS_CACHE", {})


def test_robots_allows_when_robots_txt_is_absent(monkeypatch):
    """A 404 (no robots.txt at all) is not the same as an explicit
    Disallow — every existing provider here was vetted against domains
    that often have no robots.txt file whatsoever."""
    _reset_robots_cache(monkeypatch)
    monkeypatch.setattr(job_scanner.SESSION, "get",
                         lambda url, timeout=None: _FakeResponse(status_code=404))
    assert job_scanner._robots_allows("https://example-tenant.oraclecloud.eu/hcmRestApi/foo") is True


def test_robots_allows_respects_an_explicit_disallow(monkeypatch):
    _reset_robots_cache(monkeypatch)
    robots_txt = "User-agent: *\nDisallow: /hcmRestApi/\n"
    monkeypatch.setattr(job_scanner.SESSION, "get",
                         lambda url, timeout=None: _FakeResponse(status_code=200, text=robots_txt))
    assert job_scanner._robots_allows("https://blocked.oraclecloud.eu/hcmRestApi/foo") is False


def _fake_oracle_page(reqs, total):
    return _FakeResponse(status_code=200, json_data={
        "items": [{"TotalJobsCount": total, "requisitionList": reqs}],
    })


def test_search_oracle_hcm_paginates_and_builds_the_job_url(monkeypatch):
    """search_oracle_hcm no longer filters by title itself — that moved to
    scan_all_companies_detailed() so every dropped title is visible for
    scan observability. This tests the raw fetch + pagination + URL shape
    only; see test_scan_all_companies_detailed_* for the centralized
    title/location filtering."""
    _reset_robots_cache(monkeypatch)
    all_reqs = [
        {"Id": "1001", "Title": "Senior Database Engineer", "PrimaryLocation": "Geneva, Switzerland", "PostedDate": "2026-08-01"},
        {"Id": "1002", "Title": "Head of Product Design", "PrimaryLocation": "Geneva, Switzerland", "PostedDate": "2026-08-05"},
        {"Id": "1003", "Title": "Tax Specialist", "PrimaryLocation": "Zurich, Switzerland", "PostedDate": "2026-08-10"},
    ]
    calls = []

    def fake_get(url, params=None, timeout=None):
        if "robots.txt" in url:
            return _FakeResponse(status_code=404)
        calls.append(params)
        offset = int(params["finder"].split("offset=")[1])
        # Two pages of 2, matching the real pagination loop.
        page = all_reqs[offset:offset + 2]
        return _fake_oracle_page(page, total=len(all_reqs))

    monkeypatch.setattr(job_scanner.SESSION, "get", fake_get)

    results, summary = job_scanner.search_oracle_hcm(
        "UBP", "https://iaadtu.fa.ocs.oraclecloud.eu", "CX_1")

    assert len(calls) == 2  # paginated: offset=0 then offset=2
    assert len(results) == 3  # raw, unfiltered
    by_id = {r["job_id"]: r for r in results}
    design_row = by_id["CX_1_1002"]
    assert design_row["title"] == "Head of Product Design"
    assert design_row["url"] == ("https://iaadtu.fa.ocs.oraclecloud.eu"
                                  "/hcmUI/CandidateExperience/en/sites/CX_1/job/1002")
    assert design_row["portal"] == "oracle_hcm"
    assert summary == "3 total"


def test_search_oracle_hcm_skips_and_logs_when_robots_txt_disallows(monkeypatch):
    _reset_robots_cache(monkeypatch)
    robots_txt = "User-agent: *\nDisallow: /hcmRestApi/\n"

    def fake_get(url, params=None, timeout=None):
        if "robots.txt" in url:
            return _FakeResponse(status_code=200, text=robots_txt)
        raise AssertionError("should never reach the API when robots.txt disallows it")

    monkeypatch.setattr(job_scanner.SESSION, "get", fake_get)

    result, msg = job_scanner.search_oracle_hcm(
        "Blocked Co", "https://blocked.oraclecloud.eu", "CX_1")
    assert result is None
    assert "robots.txt" in msg


def test_search_oracle_hcm_returns_the_one_real_posting_unfiltered(monkeypatch):
    """UBP's real first run: 48 open roles, 0 design titles — that's a
    centralized-filtering result now (see test_scan_all_companies_detailed_*),
    not something the raw provider itself decides."""
    _reset_robots_cache(monkeypatch)

    def fake_get(url, params=None, timeout=None):
        if "robots.txt" in url:
            return _FakeResponse(status_code=404)
        return _fake_oracle_page(
            [{"Id": "1", "Title": "Security Engineer", "PrimaryLocation": "Geneva", "PostedDate": "2026-08-01"}],
            total=1,
        )

    monkeypatch.setattr(job_scanner.SESSION, "get", fake_get)
    results, summary = job_scanner.search_oracle_hcm("UBP", "https://iaadtu.fa.ocs.oraclecloud.eu", "CX_1")
    assert len(results) == 1
    assert results[0]["title"] == "Security Engineer"
    assert summary == "1 total"


# scan_all_companies_detailed() — centralized title/location filtering and
# the per-company funnel scan observability reads.

def _mk(title, location):
    return {"company": "Acme", "title": title, "location": location,
            "url": "https://example.com/1", "posted": "2026-08-01",
            "portal": "greenhouse", "job_id": "1"}


def test_scan_all_companies_detailed_splits_title_and_location_centrally(monkeypatch):
    raw = [
        _mk("Head of Product Design", "Geneva, Switzerland"),   # title ok, location ok
        _mk("Head of Product Design", "Paris, France"),          # title ok, location drops
        _mk("Warehouse Associate", "Geneva, Switzerland"),       # title drops
    ]
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: (raw, "3 total"))

    results = job_scanner.scan_all_companies_detailed(
        [("Acme", "greenhouse", "unused")], TITLE_PATTERNS, LOCATION_PATTERNS)
    assert len(results) == 1
    c = results[0]
    assert c["outcome"] == "ok"
    assert c["raw_count"] == 3
    assert len(c["title_matches"]) == 2
    assert c["dropped_titles"] == ["Warehouse Associate"]
    assert len(c["location_matches"]) == 1
    assert c["location_matches"][0]["location"] == "Geneva, Switzerland"
    assert c["dropped_locations"] == ["Paris, France"]


def test_scan_all_companies_detailed_classifies_a_real_error(monkeypatch):
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: (None, "HTTP 500"))

    results = job_scanner.scan_all_companies_detailed(
        [("Acme", "greenhouse", "unused")], TITLE_PATTERNS, LOCATION_PATTERNS)
    assert results[0]["outcome"] == "error"
    assert results[0]["error_detail"] == "HTTP 500"
    assert results[0]["raw_count"] == 0


def test_scan_all_companies_detailed_never_calls_a_robots_block_an_error(monkeypatch):
    monkeypatch.setattr(job_scanner, "search_oracle_hcm",
                         lambda company, base_url, site: (None, job_scanner.ROBOTS_SKIP_MESSAGE))

    results = job_scanner.scan_all_companies_detailed(
        [("Blocked Co", "oracle_hcm", "https://x|CX_1")], TITLE_PATTERNS, LOCATION_PATTERNS)
    assert results[0]["outcome"] == "skipped_robots"
    assert results[0]["outcome"] != "error"


def test_scan_all_companies_detailed_caps_dropped_titles_at_50(monkeypatch):
    raw = [_mk(f"Warehouse Associate {i}", "Geneva, Switzerland") for i in range(60)]
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: (raw, "60 total"))

    results = job_scanner.scan_all_companies_detailed(
        [("Acme", "greenhouse", "unused")], TITLE_PATTERNS, LOCATION_PATTERNS)
    assert len(results[0]["dropped_titles"]) == 50


def test_scan_all_companies_backward_compat_wrapper_is_title_filtered_only(monkeypatch):
    """The legacy (matches, errors) shape: title-filtered like before, but
    NOT location-filtered — location filtering was never this function's
    job even before scan observability split it out."""
    raw = [
        _mk("Head of Product Design", "Paris, France"),   # title ok, would drop on location
        _mk("Warehouse Associate", "Geneva, Switzerland"),  # title drops
    ]
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: (raw, "2 total"))

    matches, errors = job_scanner.scan_all_companies(
        [("Acme", "greenhouse", "unused")], TITLE_PATTERNS, LOCATION_PATTERNS)
    assert errors == []
    assert len(matches) == 1
    assert matches[0]["title"] == "Head of Product Design"
