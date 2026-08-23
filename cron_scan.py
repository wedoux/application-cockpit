#!/usr/bin/env python3
"""
Headless scan — for cron/launchd, no Flask process required.
==============================================================
app.py's scan_into_db() already contains the whole SOURCE -> DB write path:
scan, upsert into roles, record scan_runs/scan_run_companies (scan
observability), expire stale auto-sourced rows. This calls it directly and
prints the funnel to stdout, instead of going through the HTTP API — a cron
job that depends on the Flask app already running fails silently on any
week nobody happened to have it open.

Replaces job_scanner.py's old standalone CLI entry point (deleted
2026-08-19), which wrote its results to a CSV file outside cockpit.db —
a second, disconnected pipeline outside scan observability entirely. This
is the one real scan path now; the UI's "Scan now" button and this script
both call scan_into_db().

Usage:
    python3 cron_scan.py
"""
import sys

import db as dbmod
from app import scan_into_db


def main():
    dbmod.init_db()
    result = scan_into_db(trigger="cron")
    print(result["output"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
