import re
from pathlib import Path

import pytest
import yaml

import job_scanner


CONFIG_EXAMPLE = Path(__file__).parent.parent / "config.yaml.example"


def _write_config(tmp_path, scanner_block):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({"scanner": scanner_block}))
    return p


def _valid_scanner_block(**overrides):
    block = {
        "companies": [{"company": "Acme", "handler": "greenhouse", "target": "https://example.com/acme"}],
        "title_patterns": [r"(?i)designer"],
        "location_patterns": [r"(?i)remote"],
    }
    block.update(overrides)
    return block


# load_scanner_config() — shape validation, readable failures.

def test_missing_config_file_fails_with_a_readable_message_not_a_traceback(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(job_scanner.ScannerConfigError, match="not found"):
        job_scanner.load_scanner_config(missing)


def test_missing_scanner_block_fails_cleanly(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({"model": "claude-sonnet-5"}))
    with pytest.raises(job_scanner.ScannerConfigError, match="scanner"):
        job_scanner.load_scanner_config(p)


def test_empty_companies_list_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(companies=[]))
    with pytest.raises(job_scanner.ScannerConfigError, match="companies"):
        job_scanner.load_scanner_config(p)


def test_company_entry_missing_required_key_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(
        companies=[{"company": "Acme", "handler": "greenhouse"}]))  # no "target"
    with pytest.raises(job_scanner.ScannerConfigError, match="target"):
        job_scanner.load_scanner_config(p)


def test_company_entry_not_a_mapping_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(companies=["Acme"]))
    with pytest.raises(job_scanner.ScannerConfigError, match="mapping"):
        job_scanner.load_scanner_config(p)


def test_empty_title_patterns_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(title_patterns=[]))
    with pytest.raises(job_scanner.ScannerConfigError, match="title_patterns"):
        job_scanner.load_scanner_config(p)


def test_invalid_regex_in_title_patterns_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(title_patterns=["(unclosed"]))
    with pytest.raises(job_scanner.ScannerConfigError, match="title_patterns"):
        job_scanner.load_scanner_config(p)


def test_non_string_pattern_fails_cleanly(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(location_patterns=[42]))
    with pytest.raises(job_scanner.ScannerConfigError, match="location_patterns"):
        job_scanner.load_scanner_config(p)


def test_valid_config_loads_the_expected_shape(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block())
    companies, title_patterns, location_patterns = job_scanner.load_scanner_config(p)
    assert companies == [("Acme", "greenhouse", "https://example.com/acme")]
    assert title_patterns == [r"(?i)designer"]
    assert location_patterns == [r"(?i)remote"]


# Config-driven values are actually used by the scanner, not just loaded
# and discarded — scan_all_companies_detailed() with no explicit params
# reads real values from load_scanner_config(), which reads SCANNER_CONFIG_PATH.

def test_scan_reads_companies_and_patterns_from_config_when_not_passed_explicitly(monkeypatch, tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(
        companies=[{"company": "ConfigCo", "handler": "greenhouse", "target": "unused"}],
        title_patterns=[r"(?i)designer"],
        location_patterns=[r"(?i)geneva"],
    ))
    monkeypatch.setattr(job_scanner, "SCANNER_CONFIG_PATH", p)

    raw = [
        {"company": "ConfigCo", "title": "Product Designer", "location": "Geneva, Switzerland",
         "url": "https://example.com/1", "posted": "2026-08-01", "portal": "greenhouse", "job_id": "1"},
        {"company": "ConfigCo", "title": "Warehouse Associate", "location": "Geneva, Switzerland",
         "url": "https://example.com/2", "posted": "2026-08-01", "portal": "greenhouse", "job_id": "2"},
    ]
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: (raw, "2 total"))

    results = job_scanner.scan_all_companies_detailed()  # no explicit args
    assert len(results) == 1
    assert results[0]["company"] == "ConfigCo"
    assert len(results[0]["title_matches"]) == 1
    assert results[0]["title_matches"][0]["title"] == "Product Designer"
    assert results[0]["dropped_titles"] == ["Warehouse Associate"]


def test_explicit_args_override_config_even_when_config_present(monkeypatch, tmp_path):
    """An explicit companies/title_patterns/location_patterns argument wins
    over config.yaml — real callers (app.py) rely on being able to pass
    their own values; only the CLI's bare call falls through to config."""
    p = _write_config(tmp_path, _valid_scanner_block(
        companies=[{"company": "FromConfig", "handler": "greenhouse", "target": "unused"}]))
    monkeypatch.setattr(job_scanner, "SCANNER_CONFIG_PATH", p)
    monkeypatch.setattr(job_scanner, "search_greenhouse", lambda company, url: ([], "0 total"))

    results = job_scanner.scan_all_companies_detailed(
        companies=[("Explicit", "greenhouse", "unused")],
        title_patterns=[r"(?i)designer"],
        location_patterns=[r".*"],
    )
    assert results[0]["company"] == "Explicit"


# config.yaml.example — the generic starter set ships working and produces
# a real scan against mocked HTTP responses, not just a config that parses.

def test_example_config_loads_cleanly():
    companies, title_patterns, location_patterns = job_scanner.load_scanner_config(CONFIG_EXAMPLE)
    assert len(companies) >= 1
    assert len(title_patterns) >= 1
    assert len(location_patterns) >= 1
    for company, handler, target in companies:
        assert handler in ("workday", "greenhouse", "lever", "ashby",
                            "smartrecruiters", "workable", "oracle_hcm")


def test_example_config_produces_a_working_scan_against_mocked_responses(monkeypatch):
    companies, title_patterns, location_patterns = job_scanner.load_scanner_config(CONFIG_EXAMPLE)

    def fake_greenhouse(company_name, api_url):
        return [{"company": company_name, "title": "Senior Product Designer", "location": "Remote, Europe",
                  "url": "https://example.com/1", "posted": "2026-08-01",
                  "portal": "greenhouse", "job_id": "1"}], "1 total"

    def fake_lever(company_name, api_url):
        return [{"company": company_name, "title": "Design Lead", "location": "Remote",
                  "url": "https://example.com/2", "posted": "2026-08-01",
                  "portal": "lever", "job_id": "2"}], "1 total"

    def fake_ashby(company_name, board_slug):
        return [{"company": company_name, "title": "Staff Product Designer", "location": "Remote",
                  "url": "https://example.com/3", "posted": "2026-08-01",
                  "portal": "ashby", "job_id": "3"}], "1 total"

    monkeypatch.setattr(job_scanner, "search_greenhouse", fake_greenhouse)
    monkeypatch.setattr(job_scanner, "search_lever", fake_lever)
    monkeypatch.setattr(job_scanner, "search_ashby", fake_ashby)

    results = job_scanner.scan_all_companies_detailed(companies, title_patterns, location_patterns)

    assert len(results) == len(companies)
    assert all(c["outcome"] == "ok" for c in results)
    assert sum(len(c["location_matches"]) for c in results) >= 1


# load_manual_check() — informational-only, optional, permissive by design
# (unlike load_scanner_config()'s required companies/title_patterns/
# location_patterns): a stranger's first config may not have any known
# no-API companies yet, and that's not an error.

def test_manual_check_missing_file_returns_empty_list(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    assert job_scanner.load_manual_check(missing) == []


def test_manual_check_missing_key_returns_empty_list(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block())  # no manual_check key
    assert job_scanner.load_manual_check(p) == []


def test_manual_check_missing_scanner_block_returns_empty_list(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({"model": "claude-sonnet-5"}))
    assert job_scanner.load_manual_check(p) == []


def test_manual_check_reads_the_list_when_present(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(
        manual_check=["Acme — https://careers.acme.example (SuccessFactors)"]))
    assert job_scanner.load_manual_check(p) == [
        "Acme — https://careers.acme.example (SuccessFactors)"
    ]


def test_manual_check_rejects_a_non_list(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(manual_check="not a list"))
    with pytest.raises(job_scanner.ScannerConfigError, match="manual_check"):
        job_scanner.load_manual_check(p)


def test_manual_check_rejects_non_string_entries(tmp_path):
    p = _write_config(tmp_path, _valid_scanner_block(manual_check=[42]))
    with pytest.raises(job_scanner.ScannerConfigError, match="manual_check"):
        job_scanner.load_manual_check(p)


def test_example_config_manual_check_loads_cleanly():
    manual_check = job_scanner.load_manual_check(CONFIG_EXAMPLE)
    assert len(manual_check) >= 1
    assert all(isinstance(x, str) for x in manual_check)
