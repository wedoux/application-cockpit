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

Exits non-zero if any token is found anywhere. Run it as the last action
before any commit that will be pushed, and again immediately before the
push itself — two checks because the two moments answer different
questions ("is what I'm about to commit clean" vs "is everything that's
ever going to be pushed clean").

Usage:
    python3 check_privacy.py [target_dir] [--tokens FILE]
    # target_dir defaults to this file's directory
    # --tokens defaults to <target_dir>/.privacy-tokens
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = args[0] if args else str(Path(__file__).parent)

    tokens_file = None
    for i, a in enumerate(sys.argv):
        if a == "--tokens" and i + 1 < len(sys.argv):
            tokens_file = sys.argv[i + 1]
    if tokens_file is None:
        tokens_file = str(Path(target) / ".privacy-tokens")

    tokens = load_tokens(tokens_file)

    try:
        tree_findings, history_findings = check(target, tokens, skip_paths=(tokens_file,))
    except NotAGitRepoError as e:
        _print_tree_findings(getattr(e, "tree_findings", None) or [])
        print(f"\ncheck_privacy: FAILED — history not checked: {e}")
        return 1

    if not tree_findings and not history_findings:
        print(f"check_privacy: clean — no tokens found in {target} (tree + history)")
        return 0

    _print_tree_findings(tree_findings)
    if history_findings:
        print(f"\nGIT HISTORY — {len(history_findings)} blob(s) with a hit:")
        for sha, hits in history_findings:
            print(f"  {sha}: {', '.join(hits)}")
    print(f"\ncheck_privacy: FAILED — {len(tree_findings)} tree hit(s), "
          f"{len(history_findings)} history hit(s)")
    return 1


def _print_tree_findings(tree_findings):
    if tree_findings:
        print(f"WORKING TREE — {len(tree_findings)} file(s) with a hit:")
        for path, hits in tree_findings:
            print(f"  {path}: {', '.join(hits)}")


if __name__ == "__main__":
    sys.exit(main())
