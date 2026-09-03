"""
The mechanical privacy check itself, tested — the one guardrail in this
project with the worst failure mode gets the same "prove it, don't trust
it" treatment every other gate here already has.

Tests use synthetic tokens throughout, not real personal data — the
mechanism is what's under test, not any specific identifier. That's also
why the real token list lives in a gitignored .privacy-tokens file rather
than in this module: a test fixture (or the script itself) embedding real
personal data would itself become exactly the kind of leak this exists to
catch, the moment either ships as part of the public repo.
"""
import subprocess

import check_privacy as cp

FAKE_TOKENS = ["fake.person@example.com", "Fakename Surname", "/Users/fakeuser"]


def _git(repo_dir, *args):
    subprocess.run(["git", "-C", str(repo_dir), *args], check=True,
                    capture_output=True)


def _init_repo(repo_dir):
    repo_dir.mkdir(exist_ok=True)
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")


# ------------------------------------------------------------------
# load_tokens
# ------------------------------------------------------------------

def test_load_tokens_reads_one_per_line_skipping_comments_and_blanks(tmp_path):
    f = tmp_path / ".privacy-tokens"
    f.write_text("# a comment\n\nfake.person@example.com\n  Fakename Surname  \n")
    assert cp.load_tokens(f) == ["fake.person@example.com", "Fakename Surname"]


def test_load_tokens_raises_when_file_missing(tmp_path):
    try:
        cp.load_tokens(tmp_path / "nope")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as e:
        assert ".privacy-tokens.example" in str(e) or "nope" in str(e)


def test_load_tokens_raises_on_an_all_comments_file(tmp_path):
    """The .example template copied verbatim without real values must not
    silently produce a "clean" pass with nothing actually checked."""
    f = tmp_path / ".privacy-tokens"
    f.write_text("# fill this in\n# another comment\n\n")
    try:
        cp.load_tokens(f)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no real tokens" in str(e)


# ------------------------------------------------------------------
# scan_working_tree
# ------------------------------------------------------------------

def test_scan_working_tree_finds_a_planted_token(tmp_path):
    (tmp_path / "config.yaml").write_text("email: fake.person@example.com\n")
    findings = cp.scan_working_tree(tmp_path, FAKE_TOKENS)
    assert len(findings) == 1
    path, hits = findings[0]
    assert path == "config.yaml"
    assert "fake.person@example.com" in hits


def test_scan_working_tree_is_clean_on_a_generic_fixture(tmp_path):
    (tmp_path / "config.yaml").write_text("email: jane@example.com\nname: Jane Doe\n")
    (tmp_path / "app.py").write_text("# a comment about the user's preferences\n")
    assert cp.scan_working_tree(tmp_path, FAKE_TOKENS) == []


def test_scan_working_tree_checks_binary_files_too(tmp_path):
    """A SQLite file (or any binary) stores text values as plain UTF-8
    bytes — a token embedded in one must still be caught, not skipped
    because the file "looks binary"."""
    (tmp_path / "snapshot.db").write_bytes(
        b"\x00\x01SQLite format 3\x00" + "Fakename Surname".encode() + b"\x00\xff"
    )
    findings = cp.scan_working_tree(tmp_path, FAKE_TOKENS)
    assert len(findings) == 1
    assert "Fakename Surname" in findings[0][1]


# ------------------------------------------------------------------
# scan_git_history
# ------------------------------------------------------------------

def test_scan_git_history_raises_for_a_non_git_directory(tmp_path):
    """The guard this whole script exists to enforce on itself: a target
    with no .git has no history to check, so scan_git_history() must
    refuse rather than silently return [] — an empty result here used to
    be indistinguishable from "checked, found nothing", and the first real
    run of this tool against a target still living inside a parent repo
    (pre its own `git init`) reported exactly that false "clean"."""
    (tmp_path / "file.txt").write_text("fake.person@example.com")
    try:
        cp.scan_git_history(tmp_path, FAKE_TOKENS)
        assert False, "expected NotAGitRepoError"
    except cp.NotAGitRepoError as e:
        assert str(tmp_path) in str(e)


def test_check_raises_not_a_git_repo_error_with_tree_findings_attached(tmp_path):
    """check() must not lose a real working-tree leak behind the history
    error — the caller still needs to see it, even though the overall
    result is a hard failure either way."""
    (tmp_path / "leak.txt").write_text("fake.person@example.com")
    try:
        cp.check(tmp_path, FAKE_TOKENS)
        assert False, "expected NotAGitRepoError"
    except cp.NotAGitRepoError as e:
        assert len(e.tree_findings) == 1
        assert "fake.person@example.com" in e.tree_findings[0][1]


def test_scan_git_history_finds_a_token_removed_from_the_working_tree():
    """The exact real-world scenario this script exists for: cockpit.db.bak
    and scan.log were committed with real content, then removed in a later
    commit. The working tree has been clean for weeks; the history never
    was. A tree-only check would report false confidence."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init_repo(repo)

        leaked = repo / "scan.log"
        leaked.write_text("scan for fake.person@example.com complete\n")
        _git(repo, "add", "scan.log")
        _git(repo, "commit", "-q", "-m", "baseline")

        leaked.unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "untrack scan.log")

        assert cp.scan_working_tree(repo, FAKE_TOKENS) == []  # clean now
        history_findings = cp.scan_git_history(repo, FAKE_TOKENS)  # but not ever
        assert len(history_findings) == 1
        assert "fake.person@example.com" in history_findings[0][1]


def test_scan_git_history_is_clean_on_a_generic_repo():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init_repo(repo)
        (repo / "app.py").write_text("print('hello world')\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-q", "-m", "initial")

        assert cp.scan_git_history(repo, FAKE_TOKENS) == []


# ------------------------------------------------------------------
# main() — CLI entry point, exit codes
# ------------------------------------------------------------------

def test_main_exits_nonzero_when_dirty(tmp_path, capsys, monkeypatch):
    (tmp_path / ".privacy-tokens").write_text("fake.person@example.com\n")
    (tmp_path / "leak.txt").write_text("fake.person@example.com")
    monkeypatch.setattr("sys.argv", ["check_privacy.py", str(tmp_path)])
    rc = cp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED" in out
    assert "fake.person@example.com" in out


def test_main_exits_zero_when_clean(tmp_path, capsys, monkeypatch):
    """A real repo, actually clean in both tree and history — the only
    case that should pass. tmp_path must be a real git repo here, not a
    bare directory: since the NotAGitRepoError guard landed, a non-repo
    target fails regardless of tree content (see the dedicated guard test
    below) rather than reporting a false "clean"."""
    _init_repo(tmp_path)
    (tmp_path / ".privacy-tokens").write_text("fake.person@example.com\n")
    (tmp_path / "clean.txt").write_text("nothing sensitive here")
    _git(tmp_path, "add", "clean.txt")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    monkeypatch.setattr("sys.argv", ["check_privacy.py", str(tmp_path)])
    rc = cp.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out


def test_main_fails_when_target_has_no_git_history_even_if_tree_is_clean(
    tmp_path, capsys, monkeypatch
):
    """The exact false pass this guard exists to prevent, at the actual
    CLI entry point real usage goes through: a target that isn't a git
    repo has no history to leak from, so a naive check would report
    "clean" having genuinely never looked. If the NotAGitRepoError guard
    is ever removed or bypassed, this test fails."""
    (tmp_path / ".privacy-tokens").write_text("fake.person@example.com\n")
    (tmp_path / "clean.txt").write_text("nothing sensitive here")
    monkeypatch.setattr("sys.argv", ["check_privacy.py", str(tmp_path)])
    rc = cp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "check_privacy: clean" not in out
    assert "FAILED" in out
    assert "history not checked" in out


def test_main_uses_an_explicit_tokens_file_via_flag(tmp_path, monkeypatch):
    tokens_file = tmp_path / "custom-tokens.txt"
    tokens_file.write_text("fake.person@example.com\n")
    (tmp_path / "leak.txt").write_text("fake.person@example.com")
    monkeypatch.setattr("sys.argv",
                         ["check_privacy.py", str(tmp_path), "--tokens", str(tokens_file)])
    assert cp.main() == 1


# ------------------------------------------------------------------
# Classification — would git publish this?
# ------------------------------------------------------------------
# The script used to exit non-zero on any hit anywhere. On any machine that
# has actually run the app that means permanently red — cockpit.db,
# backups/, config.yaml and .env all hold real data by design — and a gate
# that can never go green is one people learn to ignore. Hits are still all
# printed; only publishable ones fail.

def _repo_with(tmp_path, files, gitignore=None, commit=(), allowlist=None):
    """A throwaway repo. `files` is {path: text}; `commit` names which of
    them to actually track."""
    _init_repo(tmp_path)
    if gitignore is not None:
        (tmp_path / ".gitignore").write_text(gitignore)
    if allowlist is not None:
        (tmp_path / ".privacy-allowlist").write_text(allowlist)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for name in commit:
        _git(tmp_path, "add", "-f", name)
    if commit:
        _git(tmp_path, "commit", "-q", "-m", "fixture")
    return tmp_path


def _run(tmp_path, monkeypatch, capsys):
    (tmp_path / ".privacy-tokens").write_text("\n".join(FAKE_TOKENS) + "\n")
    monkeypatch.setattr("sys.argv", ["check_privacy.py", str(tmp_path)])
    rc = cp.main()
    return rc, capsys.readouterr().out


def test_a_tracked_file_with_a_hit_fails(tmp_path, monkeypatch, capsys):
    _repo_with(tmp_path, {"config.yaml": "email: fake.person@example.com\n"},
               commit=["config.yaml"])
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "TRACKED" in out and "FAILED" in out
    assert "config.yaml" in out


def test_a_history_blob_with_a_hit_fails_even_when_the_tree_is_clean(
    tmp_path, monkeypatch, capsys
):
    """The scenario the history scan exists for: committed, then deleted.
    Classification must not soften this — the blob is still reachable."""
    _repo_with(tmp_path, {"scan.log": "fake.person@example.com\n"}, commit=["scan.log"])
    (tmp_path / "scan.log").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "remove")

    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "GIT HISTORY" in out
    assert "scan.log" in out, "the blob should be named by the path it was committed under"


def test_a_gitignored_file_with_a_hit_passes_and_is_still_reported(
    tmp_path, monkeypatch, capsys
):
    """cockpit.db and .env hold real data on every working machine. Reported
    so you can see them, forgiven so the gate stays usable."""
    _repo_with(tmp_path,
               {"cockpit.db": "fake.person@example.com\n", "app.py": "print(1)\n"},
               gitignore="cockpit.db\n", commit=["app.py", ".gitignore"])
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert "GITIGNORED" in out
    assert "cockpit.db" in out, "forgiven is not the same as hidden"
    assert "PASS" in out


def test_an_untracked_unignored_file_with_a_hit_fails(tmp_path, monkeypatch, capsys):
    """The dangerous middle case, and the one a plain tracked/untracked
    split would drop: a stray notes.txt nobody has added yet is one
    `git add -A` from being published."""
    _repo_with(tmp_path, {"app.py": "print(1)\n", "notes.txt": "Fakename Surname\n"},
               commit=["app.py"])
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "UNTRACKED" in out
    assert "notes.txt" in out


def test_an_allowlisted_tracked_file_passes_but_is_still_printed(
    tmp_path, monkeypatch, capsys
):
    """An MIT licence has to carry the copyright holder's real name. The
    allowlist acknowledges that once — and keeps printing it, because the
    entry names a path rather than a value, so the file could later gain a
    token nobody intended."""
    _repo_with(tmp_path, {"LICENSE": "Copyright (c) 2026 Fakename Surname\n"},
               commit=["LICENSE"], allowlist="# the licence names its holder\nLICENSE\n")
    _git(tmp_path, "add", ".privacy-allowlist")
    _git(tmp_path, "commit", "-q", "-m", "allowlist")

    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert "ALLOWLISTED" in out
    assert "LICENSE" in out and "Fakename Surname" in out


def test_an_absent_allowlist_is_not_an_error(tmp_path, monkeypatch, capsys):
    """A fresh fork has no allowlist and must still run — unlike
    .privacy-tokens, whose absence stops the check."""
    assert cp.load_allowlist(tmp_path / "nope") == set()
    _repo_with(tmp_path, {"app.py": "print(1)\n"}, commit=["app.py"])
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_load_allowlist_skips_comments_and_blank_lines(tmp_path):
    f = tmp_path / ".privacy-allowlist"
    f.write_text("# why this one\nLICENSE\n\n  ./docs/AUTHORS.md  \n")
    assert cp.load_allowlist(f) == {"LICENSE", "docs/AUTHORS.md"}


def test_allowlisting_one_path_does_not_excuse_the_same_blob_elsewhere(
    tmp_path, monkeypatch, capsys
):
    """Identical content committed as LICENSE and as secrets.txt is one
    blob. The LICENSE entry must not launder the other path."""
    text = "Copyright (c) 2026 Fakename Surname\n"
    _repo_with(tmp_path, {"LICENSE": text, "secrets.txt": text},
               commit=["LICENSE", "secrets.txt"],
               allowlist="LICENSE\n")
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "secrets.txt" in out


def test_a_tracked_file_that_gitignore_also_matches_still_fails(
    tmp_path, monkeypatch, capsys
):
    """git tracks what it tracks; a later .gitignore pattern doesn't
    unpublish an already-tracked file."""
    _repo_with(tmp_path, {"config.yaml": "fake.person@example.com\n"},
               gitignore="config.yaml\n", commit=["config.yaml", ".gitignore"])
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "TRACKED" in out


def test_the_not_a_git_repo_guard_still_fails_hard(tmp_path, monkeypatch, capsys):
    """Classification needs git to tell published from local, so a target
    with no git can forgive nothing — and a history it never reached must
    still raise rather than report clean."""
    (tmp_path / ".privacy-tokens").write_text("fake.person@example.com\n")
    (tmp_path / "cockpit.db").write_text("fake.person@example.com")
    monkeypatch.setattr("sys.argv", ["check_privacy.py", str(tmp_path)])
    rc = cp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "history not checked" in out
    assert "cockpit.db" in out
