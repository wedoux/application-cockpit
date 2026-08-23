#!/usr/bin/env python3
"""
PDF export (Phase 3b, spec section 11).
=========================================
Headless Chromium via Playwright renders the same self-contained HTML the
/preview endpoint serves — same template, same content, so what you preview
in a browser is what you get in the PDF. A4, print backgrounds on (the
accent-blue rules, hairlines, and headshot ring are backgrounds — without
this flag Chromium's print mode drops them).

Drives the already-installed system Google Chrome (channel="chrome") rather
than Playwright's own bundled Chromium, so there's no separate ~300MB browser
download on top of the `playwright` pip package. Falls back to Playwright's
managed Chromium if Chrome isn't found — that path does require
`playwright install chromium` once, and raises a clear error naming that
command rather than failing silently.

Generated on demand, never during /generate — drafting stays fast, and a
role you never download never pays the browser-launch cost.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright


class PdfExportError(Exception):
    pass


def _launch(p):
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        try:
            return p.chromium.launch()
        except Exception as e:
            raise PdfExportError(
                "No usable browser found. Install Google Chrome, or run "
                "`playwright install chromium` in this project's .venv."
            ) from e


def html_to_pdf(html, output_path):
    """Render a self-contained HTML string to a PDF file. Raises
    PdfExportError on failure (e.g. no browser available) rather than
    leaving a partial or missing file unexplained."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.pdf(path=str(output_path), format="A4", print_background=True)
            finally:
                browser.close()
    except PdfExportError:
        raise
    except Exception as e:  # noqa: BLE001 — surface the real cause
        raise PdfExportError(f"PDF export failed: {e}") from e
    return output_path
