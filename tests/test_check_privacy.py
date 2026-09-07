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
        assert history_findings[0]["type"] == cp.BLOB
        assert "fake.person@example.com" in history_findings[0]["hits"]


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


# ------------------------------------------------------------------
# Commit and tag messages
# ------------------------------------------------------------------
# Until 2026-09-07 scan_git_history filtered cat-file --batch-all-objects
# down to blobs, so no commit message in any repository had ever been read
# by this script. That left commit-message discipline as the one rule here
# enforced by remembering rather than by a gate. A leak in a message is as
# permanent as one in a file and harder to spot: no diff view shows it.

def _commit_with_message(repo_dir, message, filename="app.py", content="print(1)\n"):
    (repo_dir / filename).write_text(content)
    _git(repo_dir, "add", filename)
    _git(repo_dir, "commit", "-q", "-m", message)


def test_a_token_in_a_commit_message_is_found(tmp_path):
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Add scraper for fake.person@example.com's inbox")

    findings = cp.scan_git_history(tmp_path, FAKE_TOKENS)
    assert len(findings) == 1
    assert findings[0]["type"] == cp.COMMIT
    assert "fake.person@example.com" in findings[0]["hits"]


def test_a_token_in_a_commit_message_fails_the_run(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Wire up Fakename Surname's dashboard")
    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "commit message" in out
    assert "Fakename Surname" in out


def test_a_token_in_a_tag_message_is_found(tmp_path):
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "clean message")
    # -m makes an annotated tag, which is a real object with its own
    # message; a lightweight tag is just a ref and has nothing to scan.
    _git(tmp_path, "tag", "-a", "v1.0", "-m", "cut for fake.person@example.com")

    findings = cp.scan_git_history(tmp_path, FAKE_TOKENS)
    assert [f["type"] for f in findings] == [cp.TAG]
    assert "fake.person@example.com" in findings[0]["hits"]


def test_the_object_type_is_reported_distinctly_from_a_blob_hit(tmp_path, monkeypatch, capsys):
    """A hit in a file needs the file removed from history; a hit in a
    message needs the message rewritten. Flattening them into one bucket
    would hide which fix applies."""
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Import Fakename Surname's notes",
                          filename="leak.txt", content="fake.person@example.com\n")

    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1
    assert "file content" in out
    assert "commit message" in out
    assert out.index("file content") != out.index("commit message")


def test_the_commit_subject_is_shown_so_the_object_can_be_found(tmp_path, monkeypatch, capsys):
    """A bare sha sends you to `git show`; the subject says which commit."""
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Wire up Fakename Surname's dashboard")
    _, out = _run(tmp_path, monkeypatch, capsys)
    assert "Wire up Fakename Surname's dashboard" in out


def test_a_clean_message_on_a_dirty_blob_does_not_report_a_commit_hit(tmp_path):
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "add a file",
                          filename="leak.txt", content="fake.person@example.com\n")
    findings = cp.scan_git_history(tmp_path, FAKE_TOKENS)
    assert [f["type"] for f in findings] == [cp.BLOB]


# ------------------------------------------------------------------
# The author identity: name excluded, address checked
# ------------------------------------------------------------------

def test_the_deliberate_author_name_does_not_fail_every_commit(tmp_path, monkeypatch, capsys):
    """PUBLIC-REPO-EXTRACTION-PLAN.md §9.1 keeps the real NAME in history on
    purpose. Scanning it would fail every commit forever over a decision
    made knowingly — the permanently-red gate this script was fixed not to
    be. Without this exclusion the repo's own history fails 19 times."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "user.name", "Fakename Surname")
    _commit_with_message(tmp_path, "a clean message")

    assert cp.scan_git_history(tmp_path, FAKE_TOKENS) == []
    rc, _ = _run(tmp_path, monkeypatch, capsys)
    assert rc == 0


def test_the_author_address_is_still_checked(tmp_path, monkeypatch, capsys):
    """Excluding the whole header to spare the name would leave the address
    unchecked — and replacing a real address with a no-reply one is exactly
    what §9.1 did, because public history is permanent and scraped."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "user.email", "fake.person@example.com")
    _commit_with_message(tmp_path, "a clean message")

    findings = cp.scan_git_history(tmp_path, FAKE_TOKENS)
    assert [f["type"] for f in findings] == [cp.COMMIT]
    assert "fake.person@example.com" in findings[0]["hits"]
    rc, _ = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1


# ------------------------------------------------------------------
# Allowlisting a commit is impossible by construction
# ------------------------------------------------------------------

def test_a_commit_message_can_never_be_allowlisted(tmp_path, monkeypatch, capsys):
    """The allowlist names paths and a commit has none, so there is nothing
    to review an exception against. A real leak in a message must not have
    a quiet route to being forgiven."""
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Wire up Fakename Surname's dashboard")
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    # Try every plausible way someone might reach for an exception.
    (tmp_path / ".privacy-allowlist").write_text(f"{sha}\nHEAD\n.\n*\n")

    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 1, "a commit hit must fail however the allowlist is written"
    assert "allowlisted" not in out.split("commit message")[1].split("\n")[0]


def test_classify_marks_a_commit_finding_unallowlistable(tmp_path):
    _init_repo(tmp_path)
    _commit_with_message(tmp_path, "Wire up Fakename Surname's dashboard")
    tree_findings = cp.scan_working_tree(tmp_path, FAKE_TOKENS)
    history_findings = cp.scan_git_history(tmp_path, FAKE_TOKENS)

    _, history = cp.classify(tmp_path, tree_findings, history_findings, allowlist={"anything"})
    assert [f["type"] for f in history] == [cp.COMMIT]
    assert history[0]["allowlisted"] is False
    assert history[0]["paths"] == []


def test_a_blob_is_still_allowlistable(tmp_path, monkeypatch, capsys):
    """The blob path keeps working — LICENSE is the reason the allowlist
    exists at all."""
    _repo_with(tmp_path, {"LICENSE": "Copyright (c) 2026 Fakename Surname\n"},
               commit=["LICENSE"], allowlist="LICENSE\n")
    _git(tmp_path, "add", ".privacy-allowlist")
    _git(tmp_path, "commit", "-q", "-m", "add allowlist")

    rc, out = _run(tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert "file content, allowlisted" in out
