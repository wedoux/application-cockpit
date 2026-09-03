"""
Staged-finds ingest panel.
===========================
The panel replaces a hand-typed CLI invocation that failed three different
ways in a single day (bare python3, wrong staging path, and — worst —
silence when it simply wasn't run). The tests below hold the properties
that make the replacement trustworthy rather than merely convenient:

  * previewing writes NOTHING (structurally, not by counting rows),
  * the mode is sniffed from the keys and a file it can't read is an error,
    never a guess,
  * a blocked duplicate is reported, not silently dropped,
  * a crafted path can't reach a file outside staging/,
  * selection is keyed to the source file, so a concurrent insert between
    preview and apply can't shift which rows get created,
  * an applied file stops being pending.
"""
import json
import re

import pytest

import app
import db as dbmod
import prompt_assembly as pa
import staging

_WRITE_SQL_RE = re.compile(r"^\s*(insert|update|delete|replace)\b", re.IGNORECASE)


class _WriteGuardedConn:
    """Raises the moment any write-shaped SQL runs through it. Same
    wrap-don't-subclass approach as test_gmail_sweep_apply.py's guard
    (sqlite3.Connection is a builtin type and can't be class-patched)."""
    def __init__(self, real_conn):
        object.__setattr__(self, "_real", real_conn)

    def execute(self, sql, *args, **kwargs):
        if _WRITE_SQL_RE.match(sql):
            raise AssertionError(f"write statement executed during preview: {sql!r}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture(autouse=True)
def isolated_staging_dir(tmp_path, monkeypatch):
    """staging.STAGING_DIR points at the configured staging folder, which
    holds real pending finds — a test that renamed a file in there would
    make a real drop disappear."""
    d = tmp_path / "staging"
    d.mkdir()
    monkeypatch.setattr(staging, "STAGING_DIR", d)
    return d


@pytest.fixture
def client():
    return app.app.test_client()


def _stage(dirpath, name, rows):
    path = dirpath / name
    path.write_text(json.dumps(rows))
    return path


def _insert_role(company, title, status="submitted", notes=None):
    conn = dbmod.connect()
    ts = dbmod.now()
    role_id = conn.execute(
        "INSERT INTO roles (job_id, source, company, title, category, status, notes, "
        "first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"test-{company}-{title}", "manual", company, title, "Leadership", status, notes,
         dbmod.today(), dbmod.today(), ts, ts),
    ).lastrowid
    conn.commit()
    conn.close()
    return role_id


CREATE_ROWS = [
    {"company": "Xapo Bank", "title": "Head of Design", "url": "https://x/1",
     "suggested_category": "Leadership"},
    {"company": "Deel", "title": "Lead Product Designer", "url": "https://x/2"},
]
UPDATE_ROWS = [
    {"company": "Acme Corp", "title": "Head of Design", "status": "rejected", "note": "rejected 12.08"},
]


# ------------------------------------------------------------------
# Mode detection
# ------------------------------------------------------------------

def test_mode_detection_sniffs_creates_from_url():
    mode, error = staging.detect_mode(CREATE_ROWS)
    assert mode == staging.MODE_CREATE
    assert error is None


def test_mode_detection_sniffs_updates_from_status_or_note():
    assert staging.detect_mode(UPDATE_ROWS) == (staging.MODE_UPDATE, None)
    # note alone is enough — status is optional in the update schema
    assert staging.detect_mode([{"company": "A", "title": "B", "note": "n"}]) == (staging.MODE_UPDATE, None)


def test_mode_detection_ignores_the_filename(isolated_staging_dir):
    """The naming convention isn't guaranteed, so a file named like a sweep
    but shaped like creates must be read as creates."""
    _stage(isolated_staging_dir, "manual-check-sweep-2026-09-02.json", CREATE_ROWS)
    entry = staging.list_staged()[0]
    assert entry["mode"] == staging.MODE_CREATE


def test_a_mixed_file_is_an_error_not_a_guess():
    mode, error = staging.detect_mode(CREATE_ROWS + UPDATE_ROWS)
    assert mode is None
    assert "look like creates" in error and "look like updates" in error


def test_a_row_carrying_both_shapes_is_an_error():
    mode, error = staging.detect_mode([{"company": "A", "title": "B", "url": "https://x", "status": "rejected"}])
    assert mode is None
    assert "both a url and a status/note" in error


def test_an_unrecognised_row_is_an_error():
    mode, error = staging.detect_mode([{"company": "A", "title": "B"}])
    assert mode is None
    assert "neither a url nor a status/note" in error


def test_garbage_json_is_reported_per_file_not_raised(isolated_staging_dir):
    (isolated_staging_dir / "broken.json").write_text("{not json at all")
    _stage(isolated_staging_dir, "fine.json", CREATE_ROWS)
    entries = {e["filename"]: e for e in staging.list_staged()}
    assert "not valid JSON" in entries["broken.json"]["error"]
    # one broken drop must not take the readable ones down with it
    assert entries["fine.json"]["error"] is None


def test_a_json_object_instead_of_a_list_is_an_error(isolated_staging_dir):
    (isolated_staging_dir / "obj.json").write_text(json.dumps({"company": "A"}))
    assert staging.list_staged()[0]["error"] == "not a JSON list of objects"


def test_an_empty_drop_is_not_an_error(isolated_staging_dir):
    _stage(isolated_staging_dir, "nothing-found.json", [])
    entry = staging.list_staged()[0]
    assert entry["mode"] == staging.MODE_EMPTY
    assert entry["error"] is None


# ------------------------------------------------------------------
# Preview
# ------------------------------------------------------------------

def test_preview_never_writes(isolated_staging_dir, monkeypatch, client):
    """GET /api/staged is a read. A row count would only prove nothing got
    created; this intercepts every statement and fails on the attempt,
    the same structural guarantee test_create_without_apply_never_issues_
    a_write_statement holds for the CLI."""
    _insert_role("Acme Corp", "Head of Design")
    _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    _stage(isolated_staging_dir, "updates.json", UPDATE_ROWS)

    real_connect = dbmod.connect
    monkeypatch.setattr(dbmod, "connect", lambda *a, **kw: _WriteGuardedConn(real_connect(*a, **kw)))

    r = client.get("/api/staged")
    assert r.status_code == 200
    assert len(r.get_json()["files"]) == 2


def test_preview_leaves_the_file_where_it_is(isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    staging.preview(path)
    assert path.exists(), "preview must not rename — only ingest() marks a file applied"


def test_preview_reports_blocked_duplicates_rather_than_skipping_them(isolated_staging_dir):
    role_id = _insert_role("Xapo Bank", "Head of Design", status="submitted")
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)

    pv = staging.preview(path)
    assert [c["company"] for c in pv["to_create"]] == ["Deel"]
    assert len(pv["blocked"]) == 1
    blocked = pv["blocked"][0]
    assert blocked["company"] == "Xapo Bank"
    assert blocked["existing"] == {"id": role_id, "company": "Xapo Bank",
                                   "title": "Head of Design", "status": "submitted"}


def test_preview_reports_invalid_rows(isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json",
                  [{"company": "Good", "title": "T", "url": "https://x/1"},
                   {"company": "", "title": "T", "url": "https://x/2"}])
    pv = staging.preview(path)
    assert len(pv["to_create"]) == 1
    assert pv["invalid"][0]["index"] == 1
    assert "missing company" in pv["invalid"][0]["problems"]


def test_preview_indexes_rows_by_position_in_the_source_file(isolated_staging_dir):
    _insert_role("Deel", "Lead Product Designer")
    rows = [
        {"company": "Xapo Bank", "title": "Head of Design", "url": "https://x/1"},
        {"company": "Deel", "title": "Lead Product Designer", "url": "https://x/2"},   # blocked
        {"company": "", "title": "T", "url": "https://x/3"},                            # invalid
        {"company": "Synthesia", "title": "Product Designer", "url": "https://x/4"},
    ]
    pv = staging.preview(_stage(isolated_staging_dir, "creates.json", rows))
    assert [c["index"] for c in pv["to_create"]] == [0, 3]
    assert [b["index"] for b in pv["blocked"]] == [1]
    assert [i["index"] for i in pv["invalid"]] == [2]


def test_preview_update_mode_shows_the_diff(isolated_staging_dir):
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted", notes="original")
    pv = staging.preview(_stage(isolated_staging_dir, "updates.json", UPDATE_ROWS))
    assert pv["mode"] == staging.MODE_UPDATE
    row = pv["to_update"][0]
    assert row["role_id"] == role_id
    changes = {c["field"]: (c["old"], c["new"]) for c in row["changes"]}
    assert changes["status"] == ("submitted", "rejected")
    assert changes["notes"][1].endswith("[rejected 12.08]")


def test_preview_surfaces_a_row_that_changes_nothing(isolated_staging_dir):
    """gsa.plan() drops a matched row with no changes out of all three of
    its lists, so the CLI never prints it. A row disappearing between the
    file and the diff is the exact silence this panel exists to remove."""
    role_id = _insert_role("Acme Corp", "Head of Design", status="rejected")
    pv = staging.preview(_stage(isolated_staging_dir, "updates.json",
                                [{"company": "Acme Corp", "title": "Head of Design", "status": "rejected"}]))
    assert pv["to_update"] == []
    assert pv["no_change"] == [{"index": 0, "company": "Acme Corp",
                                "title": "Head of Design", "role_id": role_id}]


def test_preview_reports_unmatched_updates(isolated_staging_dir):
    pv = staging.preview(_stage(isolated_staging_dir, "updates.json", UPDATE_ROWS))
    assert pv["to_update"] == []
    assert pv["unmatched"][0]["company"] == "Acme Corp"


# ------------------------------------------------------------------
# Path safety
# ------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "../cockpit.db",
    "/etc/passwd",
    "subdir/nested.json",
    "",
])
def test_paths_outside_staging_are_rejected(bad):
    with pytest.raises(staging.StagedPathError):
        staging.resolve_staged_path(bad)


def test_a_symlink_out_of_staging_is_rejected(isolated_staging_dir, tmp_path):
    outside = tmp_path / "elsewhere.json"
    outside.write_text("[]")
    link = isolated_staging_dir / "looks-legit.json"
    link.symlink_to(outside)
    with pytest.raises(staging.StagedPathError):
        staging.resolve_staged_path(link)


def test_a_non_json_file_in_staging_is_rejected(isolated_staging_dir):
    (isolated_staging_dir / "notes.txt").write_text("hello")
    with pytest.raises(staging.StagedPathError):
        staging.resolve_staged_path(isolated_staging_dir / "notes.txt")


def test_apply_route_rejects_a_traversal_path(client, isolated_staging_dir):
    r = client.post("/api/staged/apply", json={"path": "../../../etc/passwd"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_path"


def test_apply_route_rejects_a_malformed_selection(client, isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    r = client.post("/api/staged/apply", json={"path": str(path), "selection": ["0; DROP TABLE roles"]})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_selection"
    assert path.exists(), "a rejected request must not touch the file"


# ------------------------------------------------------------------
# Apply
# ------------------------------------------------------------------

def test_ingest_creates_only_the_selected_rows(isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    result = staging.ingest(path, selection=[1])

    assert result["created"] == 1
    assert result["skipped"] == 1
    conn = dbmod.connect()
    got = [r["company"] for r in conn.execute("SELECT company FROM roles")]
    conn.close()
    assert got == ["Deel"]


def test_ingest_with_no_selection_applies_everything(isolated_staging_dir):
    staging.ingest(_stage(isolated_staging_dir, "creates.json", CREATE_ROWS))
    conn = dbmod.connect()
    n = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    conn.close()
    assert n == 2


def test_selection_survives_a_row_being_blocked_after_the_preview(isolated_staging_dir):
    """Preview and apply are two requests against a live database. If the
    Monday cron inserts a matching role in between, the plan shifts — and a
    selection keyed to plan position would create the wrong row. Keyed to
    the source file, the still-selected row is still the right one."""
    rows = [
        {"company": "Xapo Bank", "title": "Head of Design", "url": "https://x/1"},
        {"company": "Deel", "title": "Lead Product Designer", "url": "https://x/2"},
        {"company": "Synthesia", "title": "Product Designer", "url": "https://x/3"},
    ]
    path = _stage(isolated_staging_dir, "creates.json", rows)
    pv = staging.preview(path)
    assert [c["index"] for c in pv["to_create"]] == [0, 1, 2]

    # ...the cron lands Xapo between the two requests, shifting the plan.
    _insert_role("Xapo Bank", "Head of Design")

    result = staging.ingest(path, selection=[0, 2])
    assert result["created"] == 1
    assert result["blocked"] == 1
    conn = dbmod.connect()
    got = sorted(r["company"] for r in conn.execute("SELECT company FROM roles WHERE source = 'sweep'"))
    conn.close()
    assert got == ["Synthesia"], "the row selected by file index, not the one at that plan position"


def test_ingest_blocks_duplicates_and_says_so(isolated_staging_dir):
    _insert_role("Xapo Bank", "Head of Design")
    result = staging.ingest(_stage(isolated_staging_dir, "creates.json", CREATE_ROWS))
    assert result["created"] == 1
    assert result["blocked"] == 1


def test_ingest_applies_updates(isolated_staging_dir):
    role_id = _insert_role("Acme Corp", "Head of Design", status="submitted", notes="original")
    result = staging.ingest(_stage(isolated_staging_dir, "updates.json", UPDATE_ROWS))

    assert result["updated"] == 1
    conn = dbmod.connect()
    row = conn.execute("SELECT status, notes FROM roles WHERE id = ?", (role_id,)).fetchone()
    conn.close()
    assert row["status"] == "rejected"
    assert row["notes"] == "original [rejected 12.08]"


def test_ingest_refuses_a_file_it_cannot_read(isolated_staging_dir):
    path = _stage(isolated_staging_dir, "mixed.json", CREATE_ROWS + UPDATE_ROWS)
    result = staging.ingest(path)
    assert result["ok"] is False
    assert path.exists(), "an unreadable file stays pending — it needs fixing, not hiding"
    conn = dbmod.connect()
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------------------
# Applied tracking
# ------------------------------------------------------------------

def test_an_applied_file_drops_off_the_pending_list(isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    assert [e["filename"] for e in staging.list_staged()] == ["creates.json"]

    staging.ingest(path)

    assert staging.list_staged() == []
    assert not path.exists()
    assert (isolated_staging_dir / "creates.applied.json").exists()
    assert [e["filename"] for e in staging.list_staged(include_applied=True)] == ["creates.applied.json"]


def test_a_file_with_rows_left_unchecked_is_still_marked_applied(isolated_staging_dir):
    """An unchecked row is a decision, not unfinished work. Leaving the file
    pending because something was deliberately dropped means the badge
    never clears."""
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    result = staging.ingest(path, selection=[])
    assert result["created"] == 0 and result["skipped"] == 2
    assert staging.list_staged() == []


def test_marking_applied_never_overwrites_an_earlier_run(isolated_staging_dir):
    (isolated_staging_dir / "creates.applied.json").write_text("[]")
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    target = staging.mark_applied(path)
    assert target.name == "creates-2.applied.json"


def test_applied_files_are_not_counted_as_pending(client, isolated_staging_dir):
    _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    _stage(isolated_staging_dir, "nothing-found.json", [])
    d = client.get("/api/staged").get_json()
    # the empty drop is listed but doesn't nag
    assert len(d["files"]) == 2
    assert d["pending"] == 1


def test_the_full_route_round_trip(client, isolated_staging_dir):
    path = _stage(isolated_staging_dir, "creates.json", CREATE_ROWS)
    listing = client.get("/api/staged").get_json()
    entry = listing["files"][0]
    assert [c["company"] for c in entry["preview"]["to_create"]] == ["Xapo Bank", "Deel"]

    r = client.post("/api/staged/apply", json={"path": entry["path"], "selection": [0]})
    assert r.status_code == 200
    assert r.get_json()["created"] == 1

    assert client.get("/api/staged").get_json()["pending"] == 0
    roles = client.get("/api/roles").get_json()["roles"]
    assert [r["company"] for r in roles] == ["Xapo Bank"]

# ------------------------------------------------------------------
# Where the staging directory comes from
# ------------------------------------------------------------------
# A real path baked into a tracked source file is a layout leak the moment
# this repo is pushed, and it breaks the pattern everything else follows:
# real paths in gitignored config.yaml, a runnable relative default in the
# committed config.yaml.example.

def test_the_staging_dir_comes_from_config(monkeypatch):
    monkeypatch.setattr(pa, "load_config", lambda *a, **kw: {"paths": {"staging_dir": "./from-config"}})
    assert staging._configured_staging_dir() == staging.REPO_DIR / "from-config"


def test_a_relative_staging_dir_resolves_against_the_repo(monkeypatch):
    """config.yaml.example ships ./staging, so a fresh clone has to land
    inside the checkout rather than wherever the process happened to start."""
    monkeypatch.setattr(pa, "load_config", lambda *a, **kw: {"paths": {"staging_dir": "../staging"}})
    assert staging._configured_staging_dir() == (staging.REPO_DIR.parent / "staging").resolve()


def test_an_absolute_staging_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "load_config", lambda *a, **kw: {"paths": {"staging_dir": str(tmp_path)}})
    assert staging._configured_staging_dir() == tmp_path.resolve()


@pytest.mark.parametrize("config", [{}, {"paths": {}}, {"paths": None}, None])
def test_a_missing_key_falls_back_to_the_repo_default(monkeypatch, config):
    monkeypatch.setattr(pa, "load_config", lambda *a, **kw: config)
    assert staging._configured_staging_dir() == staging.REPO_DIR / "staging"


def test_an_unreadable_config_does_not_take_the_panel_down(monkeypatch, capsys):
    """The panel is one feature; a malformed config.yaml must not stop the
    cockpit importing."""
    def boom(*a, **kw):
        raise ValueError("mangled yaml")
    monkeypatch.setattr(pa, "load_config", boom)
    assert staging._configured_staging_dir() == staging.REPO_DIR / "staging"
    assert "falling back" in capsys.readouterr().out


def test_no_absolute_path_is_baked_into_the_module():
    """The regression this guards: an absolute home-directory path written
    into a tracked file, which publishes a real name and folder layout on
    the next push. check_privacy.py catches this repo-wide, but only for
    someone who has the gitignored .privacy-tokens file."""
    source = (staging.REPO_DIR / "staging.py").read_text(encoding="utf-8")
    assert "/Users/" not in source
