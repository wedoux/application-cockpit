import pytest
import requests

import app
import db as dbmod
import jd_fetch
import prompt_assembly as pa


def test_review_category_blocks_generation():
    with pytest.raises(pa.AssemblyError):
        pa.resolve_master_cv("Review", pa.load_config())


def test_missing_jd_blocks_generation():
    with pytest.raises(pa.AssemblyError):
        pa.build_user_prompt({"jd_text": ""}, "cv")


def test_writing_rules_and_about_me_verbatim_in_system_prompt():
    cfg = pa.load_config()
    _, master_cv = pa.resolve_master_cv("Leadership", cfg)
    system = pa.build_system_prompt(cfg, master_cv)
    assert pa._read(cfg["paths"]["writing_rules"]).strip() in system
    assert pa._read(cfg["paths"]["about_me"]).strip() in system


def test_status_vocabulary():
    assert set(dbmod.STATUSES) == {
        "sourced", "reviewing", "drafted", "submitted",
        "interview", "offer", "rejected", "ignored", "expired",
    }


def test_category_vocabulary():
    assert dbmod.CATEGORIES == ["Leadership", "Advisory", "Senior IC", "Review"]


JS_SHELL_HTML = """
<html><head><title>Careers</title></head><body>
<nav><a href="/">Home</a><a href="/about">About</a><a href="/careers">Careers</a>
<a href="/contact">Contact</a><a href="/blog">Blog</a><a href="/login">Login</a></nav>
<div id="cookie-banner">We use cookies to improve your experience. By continuing to
browse this site you agree to our use of cookies. Read our privacy policy and our
cookie policy for more details on how your data is processed and stored.
<button>Accept all</button><button>Manage preferences</button></div>
<div id="app"></div>
<script src="/static/app.js"></script>
</body></html>
"""

# Real pasted JD, role 27 (InvestEngine) in cockpit.db, jd_source='paste'.
REAL_JD_HTML = "<html><head><title>Head of Product Design</title></head><body><article>" + "".join(
    f"<p>{line}</p>" for line in """About InvestEngine
InvestEngine is a fast-growing UK fintech building a commission-free ETF investment platform for retail and B2B partners. We are looking for a Head of Product Design to own design as a strategic function, not an execution service.

The role
- Lead and grow the product design function; set the design vision and operating model.
- Partner with Product and Engineering leadership to shape product strategy, not just ship screens.
- Build a research and evidence practice that drives decisions across the platform.
- Bring AI-driven and data-informed thinking into the investing experience.
- Own the design system and raise the craft bar across web and mobile.

What we are looking for
- Senior design leadership experience in fintech, wealth, or regulated SaaS.
- A track record of treating UX as strategy: service design, research, org and platform thinking.
- Comfort operating with executives and influencing product direction.
- Experience building or scaling a design team and design system.
- Remote-friendly within Europe.""".splitlines() if line.strip()
) + "</article></body></html>"


def test_js_shell_fixture_rejected():
    """Nav + cookie-banner boilerplate must NOT clear SUBSTANTIAL (800) — this
    was the bug: at the old threshold (200) it did, and a JS shell could reach
    generation unseen."""
    text, _title = jd_fetch._extract(JS_SHELL_HTML)
    assert len(text) < jd_fetch.SUBSTANTIAL


def test_real_jd_fixture_accepted():
    """A genuine JD of ordinary length clears SUBSTANTIAL — the raised threshold
    doesn't collaterally reject real postings."""
    text, _title = jd_fetch._extract(REAL_JD_HTML)
    assert len(text) >= jd_fetch.SUBSTANTIAL


# Workday CXS provider — job_scanner.py's own URL builder writes stored role
# URLs as f"{domain}/en-US/{site}/job{ext_path}", and ext_path already starts
# with "/job/...", so real stored URLs carry a doubled "job/job" segment that
# must be collapsed back to Workday's true externalPath before it can be used
# against the CXS detail endpoint.

def test_workday_ext_path_collapses_doubled_job_segment():
    parts = ["job", "job", "Lausanne-Switzerland", "Sr-User-Experience-Designer_145142"]
    assert jd_fetch._workday_ext_path(parts) == "/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142"


def test_workday_ext_path_leaves_a_single_job_segment_alone():
    parts = ["job", "Basel", "User-Experience-Designer_202607-118105-1"]
    assert jd_fetch._workday_ext_path(parts) == "/job/Basel/User-Experience-Designer_202607-118105-1"


def test_workday_provider_ignored_for_non_workday_urls():
    assert jd_fetch._workday_provider("https://boards.greenhouse.io/example/jobs/123") is None


class _FakeWorkdayResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_workday_provider_extracts_job_description_from_cxs_json(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeWorkdayResponse(200, {
            "jobPostingInfo": {
                "title": "Senior Hardware Product User Experience Designer",
                "jobDescription": "<p>" + ("Logitech is the Sweet Spot. " * 60) + "</p>",
            }
        })

    monkeypatch.setattr(jd_fetch.requests, "get", fake_get)
    url = ("https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/job/"
           "Lausanne-Switzerland/Senior-Hardware-Product-User-Experience-Designer_146581")
    result = jd_fetch._workday_provider(url)
    assert result is not None
    text, title = result
    assert title == "Senior Hardware Product User Experience Designer"
    assert "Logitech is the Sweet Spot" in text
    assert calls == [
        "https://logitech.wd5.myworkdayjobs.com/wday/cxs/logitech/Logitech/job/"
        "Lausanne-Switzerland/Senior-Hardware-Product-User-Experience-Designer_146581"
    ]


def test_workday_provider_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(jd_fetch.requests, "get", lambda url, headers=None, timeout=None: _FakeWorkdayResponse(404))
    url = "https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/job/gone/Old-Posting_999"
    assert jd_fetch._workday_provider(url) is None


def test_fetch_jd_falls_through_to_paste_when_workday_lookup_fails(monkeypatch):
    """A dead Workday requisition (delisted from CXS, e.g. id=2 / req 145142
    in the live corpus) must degrade to the paste box, not raise."""
    monkeypatch.setattr(jd_fetch.requests, "get", lambda url, headers=None, timeout=None: _FakeWorkdayResponse(404))
    url = "https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/job/Lausanne-Switzerland/Sr-User-Experience-Designer_145142"
    text, title, source = jd_fetch.fetch_jd(url)
    assert source == "none"
    assert text == ""


# Title-relevance check, promoted into the accept gate. Danske Bank (real
# role id 29 in cockpit.db) cleared the 800-char threshold on pure legal/
# website boilerplate — zero of its title's significant words present. This
# is the real text that was fetched for it, used as a regression fixture.

DANSKE_BANK_TITLE = "Head of Human-Centered Design (Center of Excellence)"
DANSKE_BANK_BOILERPLATE = (
    "General\n"
    "The information and documents on this website are for information purposes only and "
    "shall not be considered as an offer nor as an invitation to subscribe to or to purchase "
    "securities or any other investment product, nor as advice within the meaning of the "
    "Markets in Financial Instruments Directive. In no event should it be considered as a "
    "solicitation of business or a public offer. As such the information and documents shall "
    "not serve as a basis for any kind of obligation, contractual or otherwise.\n"
    "The information is based on sources that are deemed to be viable. Danske Bank endeavours "
    "to ensure that the information is accurate and up-to-date, and reserves the right to make "
    "corrections to the content at any time, without prior notice. However, Danske Bank cannot "
    "guarantee that such information is complete or that it has not been modified by an outside "
    "party, by means of a virus or system intrusion, for example. No information on this website "
    "may be construed as such a guarantee.\n"
    "Liability waiver: Danske Bank or any contributor to this website shall not be liable for "
    "any specific or consequential loss or damages that result from the access to or use of, "
    "or the inability to access or use, the materials on this website.\n"
    "You are aware that the use and interpretation of this information requires specific and "
    "in-depth knowledge of financial markets and that you shall remain solely responsible for "
    "the information and results obtained on the basis of this information. Furthermore it is "
    "your responsibility to verify the integrity of any information obtained via the Internet.\n"
    "Local restrictions: the information on this website is directed at individuals and "
    "companies that due to their nationality, place of registered office, or domicile, or for "
    "other reasons are governed by the laws of a country that allows unlimited access to this "
    "website.\n"
    "You are aware that you must ensure that you are legally authorised to access this website "
    "in the country from which you are making the Internet connection.\n"
    "None of the information relating to financial instruments presented on this website, nor "
    "a copy of it, may be provided, distributed or transmitted in any way to third parties, in "
    "particular in the US, Canada or other jurisdictions in which such offers or sales "
    "promotions are not allowed, without the prior written permission of Danske Bank."
)
assert len(DANSKE_BANK_BOILERPLATE) >= jd_fetch.SUBSTANTIAL  # clears the char threshold alone


def test_title_words_present_flags_the_danske_bank_boilerplate():
    result = jd_fetch.title_words_present(DANSKE_BANK_TITLE, DANSKE_BANK_BOILERPLATE)
    assert result["total"] > 0
    assert result["hits"] == 0


def test_title_words_present_clears_a_real_jd():
    result = jd_fetch.title_words_present("Head of Product Design", REAL_JD_HTML)
    # REAL_JD_HTML is HTML, but "Head of Product Design" appears verbatim in
    # the raw markup too — enough to prove the check isn't spuriously zero.
    assert result["hits"] > 0


def _insert_role_for_title_gate(title):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, "
        "jd_source, first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-title-gate-{title}", "manual", "Test Co", title, "Review", "sourced",
         "none", dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


def test_fetch_jd_accept_refuses_a_title_mismatch_without_force():
    role_id = _insert_role_for_title_gate(DANSKE_BANK_TITLE)
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/fetch_jd/accept",
                     json={"text": DANSKE_BANK_BOILERPLATE})
    d = r.get_json()
    assert r.status_code == 409
    assert d["error"] == "title_mismatch"
    assert d["title_check"]["hits"] == 0

    conn = dbmod.connect()
    row = conn.execute("SELECT jd_text FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["jd_text"] is None


def test_fetch_jd_accept_stores_a_title_mismatch_when_forced():
    role_id = _insert_role_for_title_gate(DANSKE_BANK_TITLE)
    client = app.app.test_client()
    r = client.post(f"/api/roles/{role_id}/fetch_jd/accept",
                     json={"text": DANSKE_BANK_BOILERPLATE, "force": True})
    d = r.get_json()
    assert r.status_code == 200
    assert d["ok"] is True

    conn = dbmod.connect()
    row = conn.execute("SELECT jd_text FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["jd_text"] == DANSKE_BANK_BOILERPLATE


def test_fetch_jd_accept_stores_a_real_jd_without_force():
    role_id = _insert_role_for_title_gate("Head of Product Design")
    client = app.app.test_client()
    real_text = jd_fetch._extract(REAL_JD_HTML)[0]
    r = client.post(f"/api/roles/{role_id}/fetch_jd/accept", json={"text": real_text})
    d = r.get_json()
    assert r.status_code == 200
    assert d["ok"] is True
