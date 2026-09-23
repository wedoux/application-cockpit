# Application cockpit

A local tool for running a senior job search properly: source roles, screen them before
spending anything, draft a tailored CV and cover letter, verify both against your real
history, and render a PDF that looks designed rather than parsed.

It never submits anything. There is no code path that could.

![The role table, showing the status palette from sourced through offer, rejected, and expired](docs/images/status-palette.png)

## Why I built it

I'm a design leader searching for Head of Design roles in Swiss fintech, and the part
that wore me down wasn't writing applications. It was re-explaining myself. Every
tailored CV meant pasting my background, my writing voice, and the job description into
a fresh chat that had forgotten all three, then checking the output line by line for
things I never actually did.

Most tools in this space solve a different problem. They optimise for volume: scrape
widely, score fast, apply often. At senior level that's the wrong shape. Roles I'd
actually take appear a few times a month, not a few times a day, and one application I
can defend in an interview is worth more than fifty I can't. So this optimises for the
opposite thing. Fewer applications, each one checkable.

## How it works

```
SOURCE  ──▶  GATE  ──▶  GENERATE  ──▶  VERIFY  ──▶  RENDER  ──▶  TRACK
```

| Stage | Owner | What it does |
|---|---|---|
| Source | `job_scanner.py`, `staging.py` | Seven ATS providers: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Oracle HCM. Which companies, which titles, which locations are all config, not code. Roles found elsewhere — a manual add, or a scheduled task that stages JSON for the ingest panel to apply — arrive through the same duplicate check. |
| Gate | `prompt_assembly.py`, `language_gate.py` | Screens the role before a single token is spent. |
| Generate | `generation.py`, `cv_schema.py`, `cover_letter_schema.py` | Your master CV, writing rules and profile stay resident, and the category picks which master CV and which positioning the model is told to lead with. The CV comes back as structured JSON, not prose, so tailoring can't break the layout. The cover letter has two paths (below). |
| Verify | `verifier.py`, `numeric_fact_gate.py`, `style_gate.py` | Checks the draft against your actual history, and against your own writing rules. |
| Render | `cv_render.py`, `pdf_export.py`, `ats_verify.py` | Jinja to HTML to headless Chrome to a two-page A4 PDF, with the fonts embedded. |
| Track | `db.py` | SQLite. Every scan, every decision, every document version. |

**Cover letters have two modes,** set by `cover_letter.mode` in `config.yaml`:

- `freeform` (the default) is one call: resident context, the job description, the CV that
  was just tailored for this role, and a brief.
- `plan_then_write` is two calls. A small forced tool call emits a plan first: the job
  description's own requirements quoted from the posting, two or three proof points that
  each name the requirement they answer and the CV line they're evidenced by, and a word
  target. The plan is validated (does that requirement really appear in the posting, does
  that CV line really exist) before a second free-prose call writes the letter from it.

It ships off, permanently, and [What it doesn't do](#what-it-doesnt-do) says why. The
second path is kept reachable and tested rather than deleted, because the measurements
that rejected it are worth being able to reproduce.

![A generated CV rendered to its final form, showing the Projects section](docs/images/cv-preview.png)

## The part I care about

Every tool I looked at promises it won't auto-submit your applications, and every one of
them enforces that promise with a sentence in a prompt file. A sentence in a prompt file
is a request. A different model, a jailbreak, or a bad day and the promise is gone.

The guarantees here are code. Not better-worded instructions, code that either runs or
doesn't:

**Language gate.** Blocks generation when a posting requires a language you haven't
declared, and quotes the requirement back at you. Warns, without blocking, when you've
declared it below the level asked for. Swiss postings ask for German or French
constantly and the answer is knowable before you spend anything.

![The language gate blocking generation, quoting the German requirement verbatim from the posting](docs/images/language-gate.png)

**Numeric fact gate.** Every number in a generated draft has to trace back to your master
CV, the job description, or your profile. One that doesn't is a hard block with the
unmatched figures listed. Team sizes and percentages are where interview liability
lives, and a model will invent them politely.

![The numeric fact gate blocking a draft, naming the unmatched number](docs/images/numeric-fact-gate.png)

**ATS text-layer gate.** Extracts the rendered PDF's text with `pdftotext` and refuses
the download if the email and phone number aren't in it as literal text, if the reading
order doesn't match the page, or if the extraction is full of `(cid:*)` garbage. A CV
that looks perfect and parses as nothing is a real way to lose applications quietly.

**Category and JD gates.** No mapped CV for the category, or no job description stored,
and generation isn't attempted at all. No API call happens.

The source-fidelity check is deliberately advisory rather than blocking: it flags names
and claims that don't appear in your CV, and you decide. Not everything should be a wall.

### Four times a control was not controlling anything

The gates above are the easy half. The harder lesson came from the gates themselves. Four
times in this project, something that looked like a safeguard turned out not to be one, and
not one of them announced it.

**The writing rules were loaded into every call and ignored in every letter.** My writing
rules go verbatim into the system prompt. They name "it's not X, it's Y" as a construction
to kill. Every cover letter the tool had ever produced used it anyway: sixteen letters,
sixteen violations, and sixteen over the word ceiling at a median of 538 words against a
300-word target. The instruction was sitting in the context window being ignored, quietly,
every time. Found by scanning the stored letters instead of trusting the prompt.

**The CI workflow had never run.** A committed workflow ran `pip-audit` and the full suite
on every push, with a commit message explaining why that mattered before merging a
dependency bump. It had never executed. Not once, on any commit, because Actions was
disabled at the repository level. The file sat there looking like a gate while every push
went in unchecked. Found by querying the Actions API instead of reading the YAML.

It could not have passed either. The workflow never created a `config.yaml`, so 46 tests
would have answered 500 on any run it ever made. The first fault hid the second for as long
as it lasted, and turning Actions on is what exposed it: one broken control was concealing
another inside the same file.

**A check called a kept promise broken.** A later gate verifies that a generated letter
actually delivers the figures its own plan committed to. It scored a batch and reported one
commitment dropped in two runs out of five. Both letters had delivered it, spelled out as
"eighteen designers" rather than "18". The check was wrong, and it had fired corrective
retries against letters that were already correct, which is worse than not checking at all.
Found by reading the letters it flagged instead of the score it produced.

**An experiment reported success over a traceback.** The script producing those numbers
exited zero while dying on a Python exception, because the shell reported the exit status
of the `tee` it was piped into rather than the program's. A failed experiment that reports
success is exactly how a non-result becomes a result.

Three of those four were emitting confident, plausible output the entire time they were
broken. Every one was caught by checking the control itself rather than reading what it
said. That is the actual thesis of this repository, and it is why the checks here have their
own tests, why the eval harness and the live path import the same module instead of keeping
two copies, and why the experiments below were pre-registered.

The first one is also the reason `style_gate.py` exists: the rules stopped being an
instruction and became code. Banned constructions as regexes, em-dash counts, cursed
vocabulary, word count. One implementation, imported by both the live generation path and
the offline harness, because two copies drift and then whether a draft is acceptable starts
depending on which door it came through.

**An explicit schema field did what the instruction could not.** Those sixteen letters were
all told "one page" in the prompt. All sixteen ignored it, by a stable margin, which is the
tell that a prompt line is doing no work. Moving the same constraint into a required
`target_words` field on a structured plan, with a retry behind it, landed every letter
inside its band, and the retry never fired once. The field alone did it. That was measured
on the generation path described below, which is now switched off for unrelated reasons, so
read it as a finding about instructions versus schemas rather than as something running
today.

**Some checks only exist at corpus level.** A phrase repeated across letters to different
companies is invisible to any check that reads one document at a time, because each letter
is individually fine. The cross-letter check compares a new draft against every stored
letter and reports phrases that recur. It found a real one on its first run: a twelve-word
description of the same side project, word for word, in letters to two unrelated companies.

**The experiments were pre-registered.** Before each run I wrote down what result would
justify shipping the change and what result would not, then ran it and read the output
against what I'd already committed to. This is the part I'd keep if I kept one thing. One
run came back passing three of four criteria, with the metrics looking like a clear win.
The fourth criterion was a human read of the actual letters, and it had veto power on
purpose. The letters had quietly dropped real evidence: a people-management proof point on
a role that explicitly asked for people management. Without the veto written down in
advance, a three-of-four scorecard is very easy to ship.

The same discipline is what ended the work. The final experiment's criteria, its numeric
bar and its three possible outcomes, one of which was "abandon", were written and saved
before the run made a single API call. The result matched the abandon branch, so that is
what happened.

## What it doesn't do

No auto-submit, no form filling, no LinkedIn scraping, no aggregator support.

That last one wasn't laziness. I looked at adding Switzerland's two largest job boards
and stopped: their `robots.txt` disallows the API path, their terms of use prohibit
automated access, and a single unauthenticated request came back classified as a bot.
Three signals, one answer. `robots.txt` is checked at request time for Oracle HCM, which
is the newest provider, and not yet for the other six.

Five more, all measured rather than assumed:

**`plan_then_write` was built, measured across two experiments, and rejected.** It ships
behind a config flag that defaults to off, and that is permanent rather than provisional.

It did fix what it was built to fix. Proof-point selection answers to the posting's own
stated requirements instead of defaulting to whichever facts sound most impressive, and
across fifteen letters written from three fixed plans, every checkable commitment reached
the prose in five runs out of five. Coverage became reliable.

Texture did not. Sentence-length variance, the mechanical proxy for prose that reads
written rather than assembled, came in at 10.81 against a pre-registered bar of 11.61 and a
freeform baseline of 11.58. The previous experiment scored 10.77 on the same roles. Two
experiments, no movement.

The run's own data showed why, and it was the thing I'd predicted would go wrong. Splitting
the fifteen letters by whether the correction loop fired, the letters that went through it
average 1.05 points lower variance than the letters that did not. The loop that makes the
commitments land is itself flattening the prose. Even the best case, every letter written
once with no correction pass at all, averages 11.30 and still misses the bar, so a cleaner
re-run could not have rescued it.

That is the trade this architecture makes: precision paid for in texture. It's the wrong
one for prose that gets hand-edited anyway, so the code stays behind its flag, tested and
reachable, and `freeform` stays the default.

**The commitment check is dormant.** It verifies that a letter delivered the figures its
own plan committed to, and it only runs on the plan path. The plan path is off. So in the
default configuration nothing checks whether a letter kept its promises, because in the
default configuration there is no plan for it to check against. The code is there, it has
tests, and it does not run.

**The duplication numbers are a floor and a ceiling, not a score.** The exact-overlap
measure only catches near-verbatim reuse, so it reads low on a paragraph that restates a
CV bullet in different words, which is the failure that actually matters. The similarity
measure catches that but can't tell genuine restatement from two paragraphs that merely
share a topic. Reported separately, on purpose. Collapsing them into one number would
imply a precision neither one has.

**No company-fact retrieval.** The letter knows what's in the posting and what's in your
CV, and nothing else. It can't tell you what the company shipped last quarter. Mining the
posting itself turned out to recover more than expected, which is the main reason a fetcher
hasn't been built yet.

**No outcome tracking.** The database records every draft, every gate result and every
decision, and nothing at all about whether an application got a reply. So none of the
quality measurement here is validated against the only outcome that matters. Everything
above is "does this match the standard I set", never "does this work".

## Quickstart

```bash
git clone <your-fork>
cd application-cockpit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # PDF rendering
brew install poppler                 # or: apt install poppler-utils

cp config.yaml.example config.yaml
cp .env.example .env                 # add your ANTHROPIC_API_KEY

python -m pytest                     # 512 passed, 3 skipped
python app.py                        # http://127.0.0.1:8766
```

Optional, macOS only: `bash launcher-src/build.sh` compiles the double-clickable
`Application Cockpit.app`. The compiled binary isn't committed — it would be unsigned,
so Gatekeeper would block it on your machine anyway, and you couldn't check it matched
the Swift source next to it.

The database creates itself on the first request, however you serve the app. Out of the
box it ships a sample profile, a fictional master CV, and three example companies, so a
first scan returns real postings before you've configured anything. When I ran it while
writing this, three companies returned seventeen matches.

## Making it yours

Everything personal lives in files git ignores. Point `config.yaml` at your own CV,
writing rules and profile, replace the company list and title patterns in the `scanner:`
block, and your data never enters a commit. There's no separate personal build. I run
the same code you do, it just reads different files.

Two things worth knowing before you rely on it. Title patterns are yours to tune, and
the scan report shows you every title it threw away so you can tune them against
evidence rather than guesswork. Mine were written in English and were silently dropping
"Manager, Product UX Design" until I looked. And the master CV is the verifier's ground
truth, which means anything you put in it is treated as fact from then on. Edit it
deliberately.

![The scan report with a dropped-titles row expanded, showing which postings a title pattern discarded](docs/images/scan-report.png)

## Credits and licence

This project came out of the [UX for AI certification](https://uxforai.com/c/certification),
co-hosted by Greg Nudelman and Daria Kempka. The scanner that seeded it was Greg's, given
to me during the course, and published here with his blessing.

MIT, with one exception documented in [CREDITS.md](CREDITS.md): five of the provider
handlers began as that scanner Greg wrote for his own search and gave me directly.
They're still close to what he wrote, and they aren't mine to relicense.

`CREDITS.md` also records what I took from two MIT-licensed projects, and what I only
took the idea of.
