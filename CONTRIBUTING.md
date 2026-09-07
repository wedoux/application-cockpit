# Contributing

This is a personal tool. I built it for my own job search and published it because parts of
it might be useful to someone doing the same thing. It isn't a product, and I'm not looking
for maintainers. Issues and pull requests are welcome, but may sit for a while or get declined
for reasons that only make sense inside my own search. Fork it instead if that's easier — no
permission needed, and a fork going its own way beats a PR that waits.

## Five things that will bite you

Each of these is here because it already went wrong.

**Use `./restart.sh`, never `python app.py`, once you're changing code.** The Quickstart's
`python app.py` is right for running it. Flask serves this app with no reloader, so an edited
file changes nothing until the process restarts, and a stale process answering with old code
has produced false test passes twice in this build's history. `restart.sh` kills the old
process, waits for the port, and prints the git sha it's serving.

**Don't work around `tests/conftest.py`.** Every test gets a throwaway database, and a guard
raises `RealDatabaseAccessError` if anything opens the real `cockpit.db` during a run. It's
strict because `db.connect()` once bound `DB_PATH` as a default argument evaluated at import
time: pointing tests at a temp file silently did nothing, and "isolated" tests wrote to live
data. If the guard fires, something is bypassing `db.connect()`, and that's the bug.

**Call `db.backup_db()` before any bulk mutation of roles.** Every existing write path does —
the scan, the tracker reconcile, the duplicate merge, `gmail_sweep_apply.py`, the ingest panel.
`cockpit.db` is gitignored, so those snapshots are the only route back from a bad write. Pass
a reason string; it lands in the filename, which is how you find the right one.

**Everything reaching `cockpit.db` goes through `gmail_sweep_apply.py`'s plan/apply
functions.** Don't add a second duplicate-detection rule. `staging.py` calls into them rather
than reimplementing, so a role blocked in the ingest panel is blocked by the same
`app._find_duplicate` the manual-add button uses. Two rules would drift, and what counted as a
duplicate would start depending on which door a role came through.

**Copy `.privacy-tokens.example` to `.privacy-tokens` and run `check_privacy.py` before
pushing a fork.** It reads the working tree as raw bytes, so it sees inside a SQLite snapshot
or a PDF that a text diff reports only as "binary files differ", and it reads git history the
same way — file content, commit messages and tag messages, which a diff view never shows you.
Hits in gitignored files are reported without failing — your `cockpit.db` and `config.yaml`
hold real data by design. A non-zero exit means something publishable has a hit: tracked,
reachable in history, or untracked and not ignored. Deliberate exceptions go in
`.privacy-allowlist`, which drops them from the exit code and keeps printing them anyway. A
leak found after a push can't be unpublished.

353 tests, about a minute (`python -m pytest`). Green before your change, green after.

## What I'll decline

**Auto-submit, form filling, anything that applies on your behalf.** The README's "there is
no code path that could" is a guarantee rather than a current default, and the rest of the
design leans on it.

**Aggregator scraping.** I looked into it and stopped: their `robots.txt` disallows the API
path, their terms prohibit automated access, and a single unauthenticated request came back
classified as a bot.

**Volume features** — bulk apply, queue-everything, anything making it cheaper to send more.
The design premise is fewer applications, each one checkable.

## Adding an ATS provider

This is the contribution I'd most like to get. Write `search_<portal>(company_name, target)`
returning `(raw, msg)`, then add a branch to
the dispatch in `scan_all_companies_detailed`. Copy `search_workable` or `search_oracle_hcm`
as your model — read the boundary note below before copying one of the other five. Targeting
lives in `config.yaml` under `scanner.companies` (each entry a `company`, `handler` and
`target`), never in code, and the committed `config.yaml.example` should gain nothing a
stranger can't actually run.

Verify against evidence. Run a scan, then open the scan report from the status bar: each
company gets a row showing raw → title → location → inserted, and the dropped-titles and
dropped-locations toggles list the strings that were filtered out. That's how you tell a
provider returning nothing from one whose postings all got filtered away. Existing coverage
is in `tests/test_job_scanner.py` and `tests/test_scanner_config.py`.

## One boundary to know about

`job_scanner.py` isn't uniformly mine. Five functions — `search_workday`, `search_greenhouse`,
`search_lever`, `search_ashby`, `search_smartrecruiters`, roughly 193 lines — began as a
scanner Greg Nudelman wrote for his own search and gave me directly. They're here by his
written permission, not under this repository's MIT licence. If you touch those five, or
rewrite them to drop the dependency, update `CREDITS.md` in the same commit. That boundary is
recorded in one file you might otherwise never open, which is why it's repeated here.
