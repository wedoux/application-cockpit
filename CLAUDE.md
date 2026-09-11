# CLAUDE.md

Guidance for Claude Code when working in this repo.

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
