# Credits

Third-party logic ported into this codebase, with attribution. Ideas-only
influences (no code copied) are also recorded here so the boundary is
explicit and doesn't have to be re-derived later.

## Pending — not yet cleared

### Greg Nudleman — Cybersecurity AI PM Scanner, five provider handlers
**Where:** `job_scanner.py`
**Source:** a personal job scanner Greg wrote for his own cybersecurity/AI PM
search and gave directly to the author — not a public repository, no license
attached, came with his own README documenting the tool.

**Scope, both readings, because both are true:** `job_scanner.py` itself is
roughly 30% his — five functions (`search_workday`, `search_greenhouse`,
`search_lever`, `search_ashby`, `search_smartrecruiters`; ~193 of the file's
646 lines) still close to verbatim, doing the actual work of talking to five
ATS providers. Measured against the whole project — the Flask application,
the database, generation, every gate, rendering — his code is well under 1%
of the repository. Both numbers are accurate; a reader should get both, not
whichever one flatters the provenance story.

**What was ported:** the five functions above — API request shape, JSON
field mapping, pagination, URL construction, all his. Retargeted by removing
his cybersecurity/PM company list and title patterns in favor of different
search targets, fixing a Workday query bug his version had (a role-specific
search term hardcoded server-side), and adding a location filter he didn't
need. His conservative expiry design — never mark a listing expired just
because that company's API errored — was also carried forward as an idea,
though the code implementing it has since been fully rewritten.

**What was NOT ported:** two other handlers (Workable; Getro, since deleted
as dead code) were added new during retargeting, following his pattern but
not his code. His company list, title patterns, and entire CSV-based
orchestration (run loop, load/save CSV, desktop notification) are gone,
replaced by config-driven targeting and a SQLite database. Everything added
since — an 8th provider (Oracle HCM), `robots.txt` compliance checking, scan
observability, config-driven loading — has no relationship to his code.

**Also relevant:** a CSV of his own real job-search history, kept in this
project for reference since the original handoff, was found still present
and has been removed before any wider sharing.

**Status:** PENDING. Not covered by this repository's LICENSE. A personal
gift between colleagues, not code released under any license — permission
has been asked but not yet confirmed. If declined, the five functions above
need independent rewriting before this repository can rely on this license
covering all of its own code.

## Ported (code, with attribution)

### career-ops — numeric normalization
**Where:** `Job search tool/numeric_fact_gate.py`
**Source:** [santifer/career-ops](https://github.com/santifer/career-ops), `verify-cv-facts.mjs`
**License:** MIT (Copyright (c) 2026 Santiago Fernández de Valderrama)
**What was ported:** the thousands-grouping comma-stripping regex approach
from `normalizeClaim()` — strip a comma only when followed by exactly three
digits, so "16,181" and "16181" compare equal without mangling a genuine
decimal like "2.5". Adapted from JS to Python; the magnitude-suffix
boundary rule (a suffix like "k"/"m"/"b" must end the token, so "50kg"
isn't misread as "50k") follows the same logic career-ops' `COUNT_CLAIM_RE`
uses.

**What was NOT ported, and why:** career-ops' actual gating policy —
extracting a number only when it sits next to one of a closed list of
"metric nouns," then running a separate warn/block policy layer on top — is
a different design from this project's gate, which checks every numeric
token unconditionally and blocks unconditionally. The non-ASCII digit
folding (Arabic-Indic, Devanagari, full-width, etc.) wasn't ported either;
out of scope for English-language CVs. The employer/title fact-claim
extraction in the same file wasn't touched — this project's gate is numbers
only, per its own brief.

## Ideas only — no code copied

These informed the *decision* to build a numeric fact gate (two independent
projects converging on the same control was the signal), not any specific
implementation.

- **AutoApply** — PolyForm Noncommercial license. Not compatible with this
  repo's terms; nothing from it was read as source or copied.
- **AIHawk** — bespoke non-commercial license. Same treatment: idea
  acknowledged, no code referenced.
- **JobSearch-Agent** — GPL-3.0. Explicitly avoided: copying GPL-licensed
  code into this repo would relicense the whole project under the GPL by
  accident. Nothing from it was read as source or copied.
- **ai-job-search** — [mlorentzen/ai-job-search](https://github.com/mlorentzen/ai-job-search),
  MIT (Copyright (c) 2026 Mads Lorentzen). Its source was never read directly
  by this codebase — the ideas below were specced out in prose in a separate
  planning session and implemented from that description, the same
  arm's-length relationship this project has with AutoApply/AIHawk above,
  not the direct-port relationship career-ops has. Two concepts:
  - **The Language Gate's not-declared-vs-below-declared-level split**, from
    `.claude/skills/job-application-assistant/04-job-evaluation.md`'s own
    "Language Gate" section: a language missing from the candidate's
    declared table entirely is a hard stop, while a declared language whose
    posting-stated bar reads higher than the declared level is a flag that
    still lets the draft through. `language_gate.py`'s BLOCK/WARN/NOTE
    three-way split is this same shape, extended with a third tier
    (nice-to-have/ambiguous mentions → NOTE) that ai-job-search's two-tier
    FAIL/FLAG/PASS model doesn't have, and built to read the requirement
    across four languages (English/French/German/Italian) rather than one.
  - **The ATS text-layer checklist's specific check items**, from
    `CLAUDE.md`'s "ATS & keyword verification (CV)" section: no `(cid:*)`
    markers or replacement characters in the extracted text, email/phone
    present as literal text rather than carried only by an icon glyph or a
    hyperlink, and reading order matching visual order. `ats_verify.py`'s
    `check_*` functions check exactly these three conditions.
  - **What was NOT taken from it**: the choice to extract via `pdftotext`
    (poppler) rather than `pypdf` looks like it matches ai-job-search's own
    tool choice, but it isn't borrowed — this project arrived at `pdftotext`
    independently, from a Phase 3c finding that `pypdf` silently drops text
    runs against this project's embedded variable Inter font (see
    `tests/test_pdf_export.py` and `ats_verify.py`'s own module docstring).
    Convergent tool choice, not a copied decision.
