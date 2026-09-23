# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Things that are expensive to rediscover

**`style_gate.py` has exactly one implementation and three callers.** The live generation
path (`app.py`'s `run_fidelity_gates`) and both offline eval harnesses
(`cover_letter_experiment.py`, `cover_letter_reliability.py`) import it. That is deliberate. Do not fork the
patterns into a second copy for the harness, or tune one without the other: two copies
drift, and then whether a draft is acceptable starts depending on which door the text
came through. Same reasoning as `staging.py` calling into `gmail_sweep_apply.py` instead
of reimplementing the duplicate rule.

**The negative-parallelism categories are mutually exclusive by construction.** Each
matched span belongs to exactly one category, enforced by a negative lookahead:
`_COMMA_NOT_RE` excludes `not just` precisely so `_COMMA_NOT_JUST_RE` owns those spans
alone. This is what makes the counts sum correctly instead of double-counting one
sentence under two headings. It is also exactly the property a well-meaning refactor
breaks silently, because nothing looks wrong afterward, the totals are just quietly
inflated. If you widen one pattern, check it doesn't start claiming spans another one
already owns.

**Cover letters have a mode flag: `cover_letter.mode` in `config.yaml`.** `freeform` is a
single call from a prose brief. `plan_then_write` is a validated structured plan followed
by a free-prose write step. `freeform` is the default **on purpose**, and after three
experiments that is a settled decision rather than a pending one.

`plan_then_write` works at what it was built for. Selection became correct and stayed
correct, and in the final run every checkable commitment reached the prose in 5 of 5 runs
across three fixed plans. It was rejected on texture: sentence-length variance measured
10.81 against a pre-registered bar of 11.61 and a freeform baseline of 11.58, unchanged
from the previous experiment's 10.77. Letters that went through the commitment-retry loop
averaged 1.05 points lower variance than letters that did not, so the correction loop is
part of what flattens the prose, and even a retry-free best case (11.30) misses the bar.

Do not flip the default, and do not re-open this without new evidence about texture
specifically. Both paths stay reachable so the measurement can be reproduced.

**`style_gate.scan_commitment_coverage` only runs on the plan path, so it is currently
dormant.** Do not describe it in user-facing text as something the tool does. Two
properties of it were expensive to learn and are pinned by named tests: committed figures
are read from `cv_source_line` as well as `claim` (in both real failures the digits existed
only in the source line), and a figure counts as delivered when written out in words. A
digits-only version scored "a team of eighteen designers" as a dropped commitment and fired
corrective retries against letters that were already correct. Similarity scoring is
computed but must never gate: across 12 hand-labelled proof points no text-overlap measure
separated delivered commitments from dropped ones, and cosine inverted on the clearest
pair.

**Staging lives outside the repo.** `paths.staging_dir` in the gitignored `config.yaml`
is what resolves it, and the real one points outside the repository, so a path that looks
wrong relative to the source tree is probably right. An empty staged drop is listed in the
ingest panel but does not count toward the pending badge, which is intentional: a drop
that ran and found nothing is not the same as work waiting for you.

**`config.yaml` has nothing behind it.** It is gitignored, so it is not in history, not
in any commit, and not recoverable from this repository: the working copy is the only
copy. A tree-wide edit (a bulk rename, a search-and-replace across the repo, a scripted
migration) must either exclude it explicitly or back it up first. This is not
hypothetical, an agent-run substitution rewrote every company name in it during the pass
that added this line, and only caught it because the same pass happened to be looking for
company names. There is a copy at `../config.yaml.backup`, outside the repo; refresh it
after meaningful config edits, because a stale backup is its own trap.

**The history scan has a baseline, and it is not an oversight.**
`.privacy-history-baseline` lists blob SHAs that were already published in a pushed commit
before the employer-name check existed: company names in old test fixtures, two
illustrative comments. They are accepted rather than rewritten, because rewriting pushed
history breaks every clone and still does not remove the objects, which stay addressable
by SHA on the forge. The entries are keyed by exact SHA, so anything new gets a new SHA
and fails the run. Do not "clean up" this file, and do not add to it to make a fresh
failure go away, that is the one thing it must never be used for. Every path it names is
clean from the commit that introduced the check onward.

**Real data never enters committed fixtures.** `sample-profile/` is the pattern for test
and example data: a fabricated person, fabricated CV, real writing rules. `check_privacy.py`
is the check, and it reads git history as well as the working tree. If a test needs a CV
bullet or a job description, invent one; do not paste a real employer's name into a test
file or a doc that ships.

## Dependency security

- Before adding or upgrading any dependency, check it for known vulnerabilities
  (`uvx pip-audit -r requirements.txt`, or `pip-audit` if it's already on PATH).
  Don't introduce a version with open advisories without flagging it to the user
  first and letting them decide.
- Keep exact version pins in `requirements.txt` (`pkg==x.y.z`, not ranges) —
  this is the existing convention here, keep it that way.
- Run `pip-audit` before any commit that touches `requirements.txt`, and at the
  start of any session where we're adding features (not just dependency bumps).
- Treat every PDF or file this app parses as untrusted input: enforce a file
  size limit and a processing timeout so a malformed or malicious file can't
  hang the app. This applies even to files the app generated itself, since a
  hang in the parsing step blocks the request either way.
