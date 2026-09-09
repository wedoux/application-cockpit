# Security

This is a local, single-user tool. It binds to `127.0.0.1` only, serves no
authentication, and is not built to be exposed to a network. Do not put it
behind a public address.

Two things it holds that are worth knowing about:

- **An Anthropic API key**, read from `.env` (gitignored). Nothing else reads
  it, and it is never written to the database, the logs or a rendered document.
- **Personal documents** — your CV, cover letters and job-search history — in
  `cockpit.db` and `applications/`, both gitignored. `check_privacy.py` exists
  to catch either of those reaching a commit; run it before pushing a fork.

## Reporting something

Use GitHub's private vulnerability reporting on this repository (Security →
Report a vulnerability). Please don't open a public issue for anything that
would expose someone's data or key.

This is a personal project with one maintainer, so expect a reply in days
rather than hours.
