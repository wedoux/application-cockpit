#!/usr/bin/env python3
"""
Mechanical privacy check — enforced in code, not diligence.
=============================================================
Every other guarantee in this project (never auto-submit, never fabricate
a number, never fabricate a relationship claim) is enforced by a gate in
code, not by a reviewer remembering to look. Personal-data leakage into a
public repo is the one guardrail here with the worst failure mode — an
identity leak can't be un-published — so it gets the same treatment: a
script that fails loudly, not a checklist item.

The token list (real name, email, phone, ...) lives in `.privacy-tokens`,
which is itself gitignored — never in this script, and never in git
history. A script whose own source embeds the personal data it's checking
for would fail on itself forever, the moment it ships as part of the
repo it's meant to protect. `.privacy-tokens.example` (committed, no real
values) shows the shape; copy it to `.privacy-tokens` and fill in your
own — same pattern as `.env`/`.env.example` already used in this project.

Scans a target directory two ways:
  1. The current working tree (every file, read as raw bytes so binary
     files — a SQLite snapshot, a PDF — are checked too, not skipped).
  2. The full git history — every blob ever committed, via `git cat-file`,
     not just `git log -p`'s text diffs (which show "Binary files ...
     differ" for a binary blob and never search its actual content). This
     is exactly how a past commit's personal data was found here in the
     first place: `cockpit.db.bak` and `scan.log` were removed from the
     working tree in a later commit, but their content is still
     retrievable from the commit that added them — `git log -p` alone
     would never have caught that.

     If target_dir isn't itself a git repository (no `.git`), the history
     scan RAISES (NotAGitRepoError) rather than silently reporting "0
     history hits". A target with no reachable history was never checked,
     not found clean — and this script's whole reason to exist is refusing
     to let "I didn't look" pass as "nothing there". This was a real gap:
     it used to return an empty list here, and the first real run of this
     script — against a target still living inside a parent repo before
     its own `git init` — reported a clean history that had simply never
     been checked.

Every hit is printed. Only some of them fail the run, because "this token
appears somewhere under this directory" and "this token would be published"
are different questions, and a gate that answers the first can never go
green on a machine that has actually run the app — cockpit.db, backups/,
config.yaml and .env all contain real data by design. A check that is
permanently red teaches you to ignore it, which is the same outcome as not
having one.

So each hit is classified by whether git would publish it:

  * TRACKED, or reachable in git history -> FAIL. This is publishable
    content, and history counts even when the working tree is clean.
  * UNTRACKED and NOT gitignored -> FAIL. One `git add -A` from being
    published. This is the dangerous middle case — a stray notes.txt with
    a phone number in it — and the reason classification isn't simply
    "tracked or not".
  * GITIGNORED -> reported, does not fail. Expected on any working machine.

Exit code is non-zero if anything in the first two classes has a hit.

`.privacy-allowlist` (committed, one path per line, # comments) acknowledges
the deliberate exceptions — an MIT LICENCE has to carry the copyright
holder's real name, and re-triaging that every run is how a gate becomes
furniture. An allowlisted path is excluded from the exit code but STILL
PRINTED, clearly marked. That matters: the allowlist names paths, not
values, so someone could later paste an email address into an allowlisted
file and it would stop failing. Keeping the hit visible is what stops the
allowlist from becoming a place things go to disappear.

Run it as the last action before any commit that will be pushed, and again
immediately before the push itself — two checks because the two moments
answer different questions ("is what I'm about to commit clean" vs "is
everything that's ever going to be pushed clean").

Usage:
    python3 check_privacy.py [target_dir] [--tokens FILE] [--allowlist FILE]
    # target_dir defaults to this file's directory
    # --tokens defaults to <target_dir>/.privacy-tokens
    # --allowlist defaults to <target_dir>/.privacy-allowlist (absent is fine)
"""

import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"}


class NotAGitRepoError(Exception):
    """Raised when a history scan is requested against a target that isn't
    itself a git repository. Never caught and turned into an empty result —
    a privacy check that can report "history clean" without ever having
    looked at a history is worse than no check at all."""


def load_tokens(tokens_file):
    """One real personal identifier per line. Blank lines and #-prefixed
    comments skipped. Raises if the file doesn't exist — no hardcoded
    fallback, on purpose: a missing token file should stop the check, not
    silently pass one with nothing to look for."""
    path = Path(tokens_file)
    if not path.exists():
        raise FileNotFoundError(
            f"{tokens_file} not found. Copy .privacy-tokens.example to "
            f"{path.name} and fill in your real identifiers — that file "
            "must stay gitignored, it's the token list itself."
        )
    tokens = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens.append(line)
    if not tokens:
        # An all-comments file (e.g. .privacy-tokens.example copied
        # verbatim without filling in real values) must not silently
        # produce a "clean" pass with nothing actually checked.
        raise ValueError(
            f"{tokens_file} has no real tokens (only comments/blank lines) "
            "— fill in your actual personal identifiers, one per line."
        )
    return tokens


def _token_hits(data: bytes, tokens):
    hits = []
    for token in tokens:
        if token.encode("utf-8") in data:
            hits.append(token)
    return hits


def scan_working_tree(root_dir, tokens, skip_paths=()):
    """Every file under root_dir, read as raw bytes. Returns a list of
    (relative_path, [tokens]) for files with at least one hit.

    skip_paths excludes specific files by resolved path — the token file
    itself must be excluded by its caller (main() does this), since it
    legitimately contains every token by definition and would otherwise
    always flag itself the moment it's dropped inside the scanned tree."""
    root = Path(root_dir)
    skip_resolved = {Path(p).resolve() for p in skip_paths}
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() in skip_resolved:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        hits = _token_hits(data, tokens)
        if hits:
            findings.append((str(path.relative_to(root)), hits))
    return findings


def scan_git_history(repo_dir, tokens):
    """Every blob ever committed in repo_dir's git history, via
    `git cat-file`, not `git log -p` — a binary blob's content never shows
    up in a text diff, only in the raw object itself. Returns a list of
    (blob_sha, [tokens]) for blobs with at least one hit.

    Raises NotAGitRepoError if repo_dir isn't a git repo at all — silently
    returning [] here would let a caller report "history clean" for a
    history it never actually reached."""
    git_dir = Path(repo_dir) / ".git"
    if not git_dir.exists():
        raise NotAGitRepoError(
            f"{repo_dir} is not a git repository (no .git found) — cannot "
            "verify history. Point this at the actual repo root, not a "
            "subdirectory still awaiting its own `git init`; a \"history "
            "clean\" result here would mean nothing was ever checked."
        )

    check = subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "--batch-all-objects",
         "--batch-check=%(objecttype) %(objectname)"],
        capture_output=True, text=True, check=True,
    )
    blob_shas = [line.split()[1] for line in check.stdout.splitlines()
                 if line.startswith("blob ")]

    findings = []
    for sha in blob_shas:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "cat-file", "-p", sha],
            capture_output=True, check=True,
        )
        hits = _token_hits(result.stdout, tokens)
        if hits:
            findings.append((sha, hits))
    return findings


# ------------------------------------------------------------------
# Classification — would git publish this?
# ------------------------------------------------------------------

TRACKED = "tracked"
UNTRACKED = "untracked"
IGNORED = "ignored"

# Which classes are publishable, and so fail the run. Gitignored files are
# reported and forgiven; everything else is one command away from a push.
FAILING_CLASSES = (TRACKED, UNTRACKED)


def load_allowlist(allowlist_file):
    """Paths whose hits are acknowledged and excluded from the exit code.
    One path per line relative to the repo root, # comments and blank lines
    skipped. A missing file is not an error — it just means nothing is
    allowlisted, which is the correct default for a fresh fork.

    Unlike .privacy-tokens this file names paths rather than personal data,
    so it's safe to commit, and it has to be: an exception nobody else can
    see is an exception nobody else can review."""
    path = Path(allowlist_file)
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line.lstrip("./"))
    return entries


def _git_bytes(repo_dir, *args, allowed_returncodes=(0,)):
    proc = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True)
    if proc.returncode not in allowed_returncodes:
        raise subprocess.CalledProcessError(proc.returncode, proc.args,
                                            proc.stdout, proc.stderr)
    return proc.stdout


def tracked_paths(repo_dir):
    """Every path git has under version control. -z because a path with a
    space or a quote in it would otherwise come back shell-quoted and stop
    matching the paths the tree scan produced."""
    out = _git_bytes(repo_dir, "ls-files", "-z")
    return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\x00") if p}


def ignored_paths(repo_dir, paths):
    """The subset of `paths` that .gitignore covers, in one batch call.
    check-ignore exits 1 when nothing matched, which is a result and not an
    error — anything above that is a real failure and propagates."""
    if not paths:
        return set()
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "check-ignore", "--stdin", "-z"],
        input=b"\x00".join(p.encode("utf-8", "surrogateescape") for p in paths),
        capture_output=True,
    )
    if proc.returncode > 1:
        raise subprocess.CalledProcessError(proc.returncode, proc.args,
                                            proc.stdout, proc.stderr)
    return {p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\x00") if p}


def history_blob_paths(repo_dir):
    """sha -> every path that blob has ever been committed under.

    Needed because the history scan finds blobs and the allowlist names
    paths. A blob with no path here is one `--batch-all-objects` reached but
    `rev-list` can't name (an unreachable object, say); it stays
    unallowlistable and therefore fails, which is the safe direction."""
    out = _git_bytes(repo_dir, "rev-list", "--objects", "--all").decode(
        "utf-8", "surrogateescape")
    mapping = {}
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            mapping.setdefault(sha, set()).add(path)
    return mapping


def classify(repo_dir, tree_findings, history_findings, allowlist=()):
    """Annotate raw findings with publishability. Returns (tree, history),
    lists of dicts — nothing is dropped, only labelled."""
    allowlist = set(allowlist)
    tracked = tracked_paths(repo_dir)
    ignored = ignored_paths(repo_dir, [path for path, _ in tree_findings])

    tree = []
    for path, hits in tree_findings:
        # Tracked wins over ignored: a file git already follows is published
        # whether or not some .gitignore pattern also happens to match it.
        if path in tracked:
            klass = TRACKED
        elif path in ignored:
            klass = IGNORED
        else:
            klass = UNTRACKED
        tree.append({"path": path, "hits": hits, "class": klass,
                     "allowlisted": path in allowlist})

    blob_map = history_blob_paths(repo_dir)
    history = []
    for sha, hits in history_findings:
        paths = sorted(blob_map.get(sha, ()))
        # Every path the blob ever lived at must be allowlisted, not just
        # one: the same content committed once as LICENSE and once as
        # secrets.txt is not excused by the LICENSE entry.
        allowlisted = bool(paths) and all(p in allowlist for p in paths)
        history.append({"sha": sha, "hits": hits, "paths": paths,
                        "allowlisted": allowlisted})
    return tree, history


def failing(tree, history):
    """The subset that makes the run non-zero: publishable and not
    acknowledged."""
    bad_tree = [f for f in tree
                if f["class"] in FAILING_CLASSES and not f["allowlisted"]]
    bad_history = [f for f in history if not f["allowlisted"]]
    return bad_tree, bad_history


def check(target_dir, tokens, skip_paths=()):
    """Returns (tree_findings, history_findings). Both empty means clean.

    Raises NotAGitRepoError if target_dir has no git history to check —
    with the already-computed tree_findings attached to the exception
    (e.tree_findings), so a caller can still surface a real working-tree
    leak instead of losing it behind the history error."""
    tree_findings = scan_working_tree(target_dir, tokens, skip_paths=skip_paths)
    try:
        history_findings = scan_git_history(target_dir, tokens)
    except NotAGitRepoError as e:
        e.tree_findings = tree_findings
        raise
    return tree_findings, history_findings


def main():
    # A flag's value is not the target: "--tokens FILE" used to leave FILE
    # sitting in the positional list, so calling this with no target_dir but
    # an explicit --tokens scanned the token file's own directory.
    flagged = {"--tokens", "--allowlist"}
    args = [a for i, a in enumerate(sys.argv[1:], start=1)
            if not a.startswith("--") and sys.argv[i - 1] not in flagged]
    target = args[0] if args else str(Path(__file__).parent)

    tokens_file = _flag_value("--tokens") or str(Path(target) / ".privacy-tokens")
    allowlist_file = _flag_value("--allowlist") or str(Path(target) / ".privacy-allowlist")

    tokens = load_tokens(tokens_file)
    allowlist = load_allowlist(allowlist_file)

    try:
        tree_findings, history_findings = check(target, tokens, skip_paths=(tokens_file,))
    except NotAGitRepoError as e:
        # No git means no way to tell a published file from a local one, so
        # nothing can be forgiven here — every hit is reported raw and the
        # run fails, same as before.
        _print_unclassified(getattr(e, "tree_findings", None) or [])
        print(f"\ncheck_privacy: FAILED — history not checked: {e}")
        return 1

    if not tree_findings and not history_findings:
        print(f"check_privacy: clean — no tokens found in {target} (tree + history)")
        return 0

    tree, history = classify(target, tree_findings, history_findings, allowlist)
    _print_report(tree, history)

    bad_tree, bad_history = failing(tree, history)
    if bad_tree or bad_history:
        print(f"\ncheck_privacy: FAILED — {len(bad_tree)} publishable file(s), "
              f"{len(bad_history)} history blob(s) with a hit")
        return 1

    ignored = sum(1 for f in tree if f["class"] == IGNORED)
    allowed = sum(1 for f in tree if f["allowlisted"]) + sum(1 for f in history if f["allowlisted"])
    print(f"\ncheck_privacy: PASS — nothing publishable has a hit "
          f"({ignored} gitignored, {allowed} allowlisted, all listed above)")
    return 0


def _flag_value(flag):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _print_unclassified(tree_findings):
    if tree_findings:
        print(f"WORKING TREE — {len(tree_findings)} file(s) with a hit:")
        for path, hits in tree_findings:
            print(f"  {path}: {', '.join(hits)}")


# Printed worst-first, so the thing you have to act on is at the top of the
# output rather than under thirty lines of expected noise.
_TREE_GROUPS = [
    (TRACKED, False, "TRACKED — published, or will be", "FAIL"),
    (UNTRACKED, False, "UNTRACKED and NOT gitignored — one `git add -A` from published", "FAIL"),
    (TRACKED, True, "ALLOWLISTED — acknowledged in .privacy-allowlist, still shown", "ok"),
    (UNTRACKED, True, "ALLOWLISTED — acknowledged in .privacy-allowlist, still shown", "ok"),
    (IGNORED, None, "GITIGNORED — not published, expected on a working machine", "ok"),
]


def _print_report(tree, history):
    for klass, allowlisted, heading, verdict in _TREE_GROUPS:
        group = [f for f in tree if f["class"] == klass
                 and (allowlisted is None or f["allowlisted"] == allowlisted)]
        if not group:
            continue
        print(f"{heading} [{verdict}] — {len(group)} file(s):")
        for f in group:
            print(f"  {f['path']}: {', '.join(f['hits'])}")
        print()

    bad = [f for f in history if not f["allowlisted"]]
    ok = [f for f in history if f["allowlisted"]]
    for group, heading, verdict in (
        (bad, "GIT HISTORY — reachable in history", "FAIL"),
        (ok, "GIT HISTORY — allowlisted, still shown", "ok"),
    ):
        if not group:
            continue
        print(f"{heading} [{verdict}] — {len(group)} blob(s):")
        for f in group:
            where = f" ({', '.join(f['paths'])})" if f["paths"] else " (no path in reachable history)"
            print(f"  {f['sha']}{where}: {', '.join(f['hits'])}")
        print()


if __name__ == "__main__":
    sys.exit(main())
