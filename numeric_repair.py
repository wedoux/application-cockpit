#!/usr/bin/env python3
"""
Targeted numeric repair — rung 2 of the numeric gate's repair ladder.
=====================================================================
The numeric fact gate blocks a draft whose numbers don't trace back to the
master CV, the JD or professional-profile.md. That is correct and does not
change. What changed on 29 Sep 2026 is what happens next.

Three drafts blocked in a row and every number in them was true; the model
had only reworded them ("700+ components" written as "over 700"). Under the
old pipeline one block ended the whole generation: a full draft was paid
for, nothing came back, and the only recourse was to regenerate and hope.
Regenerating is a gamble, because a fresh draft rewords different numbers —
which is exactly what happened, the retry fixing 700 and breaking on 35.

This module is the cheap middle step. Rung 1 (numeric_fact_gate) already
makes equivalent surface forms compare equal for free. When numbers are
still unmatched after that, this sends the model ONLY the sentences that
carry them plus the source lines that carry numbers, and asks for those
sentences back with every number either sourced or removed. Nothing else
in the draft is put at risk, and the whole draft is not paid for twice.

Everything here is pure: selection, prompt construction and splicing. The
API call lives in generation.py, which owns the client, the pricing table
and the usage accounting. The ladder that decides when to climb lives in
app.py next to run_fidelity_gates, because a repair has to be re-checked by
every gate, not just the one that flagged it.

Two rules this module exists to keep honest:

  * The corpus is built by code, never by the model. The source lines below
    are extracted from the same three texts the gate itself checks against.
    The repair prompt may correct or remove a number. It may not add a
    source, and there is no path here for it to try.
  * The bar never moves. This produces candidate text. It does not decide
    whether that text is acceptable — run_fidelity_gates does, on the
    spliced result, exactly as it would for a fresh draft.
"""

import re

import numeric_fact_gate as ng

# The model gets the number-bearing source lines, not the whole corpus: a
# master CV plus a JD is most of a context window's worth of text to spend
# on rewriting two sentences. Capped so a pathological source can't turn a
# cheap repair into an expensive one.
MAX_SOURCE_LINES = 60

REPAIR_TOOL_NAME = "emit_repaired_spans"
REPAIR_TOOL = {
    "name": REPAIR_TOOL_NAME,
    "description": (
        "Return the rewritten text for each flagged span. Every span you were "
        "given must appear exactly once in the output, identified by its id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "spans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "The id of the span you are rewriting."},
                        "text": {"type": "string", "description": "The rewritten span."},
                    },
                    "required": ["id", "text"],
                },
            },
        },
        "required": ["spans"],
    },
}

# Sentence boundary: a terminator followed by whitespace. Deliberately
# simple — the spans are spliced back by offset, so a split that is too
# coarse costs a slightly larger rewrite, never a corrupted document.
_SENTENCE_RE = re.compile(r"[^.!?\n]*[.!?]+[\s]*|[^.!?\n]+\n*")


def _mentions_any(text, numbers):
    return bool({m["value"] for m in ng.extract_numeric_mentions(text)} & set(numbers))


def flagged_sentences(text, unmatched):
    """The sentences of `text` that carry at least one unmatched number, as
    {"id", "text", "start", "end"}. Offsets are kept so the rewrite can be
    spliced back exactly where it came from, rather than by a string
    replace that could hit the same words elsewhere in the letter."""
    spans, next_id = [], 0
    for m in _SENTENCE_RE.finditer(text or ""):
        sentence = m.group(0)
        if not sentence.strip() or not _mentions_any(sentence, unmatched):
            continue
        spans.append({"id": next_id, "text": sentence.strip(),
                      "start": m.start(), "end": m.end()})
        next_id += 1
    return spans


def splice_sentences(text, spans, rewrites):
    """Put the rewritten spans back. Applied right-to-left so each splice
    can't move the offsets of the ones not yet applied."""
    by_id = {r["id"]: r["text"] for r in rewrites}
    out = text
    for span in sorted(spans, key=lambda s: s["start"], reverse=True):
        if span["id"] not in by_id:
            continue
        original = text[span["start"]:span["end"]]
        # Keep whatever whitespace the original span ended with, so
        # paragraph breaks survive a rewrite that trims them.
        trailing = original[len(original.rstrip()):]
        out = out[:span["start"]] + by_id[span["id"]].strip() + trailing + out[span["end"]:]
    return out


def _walk_strings(node, path=()):
    if isinstance(node, str):
        yield list(path), node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, (*path, i))


def flagged_json_fields(data, unmatched):
    """The string leaves of a CV's content_json that carry an unmatched
    number, as {"id", "text", "path"}.

    The CV is generated as structured JSON and rendered to markdown. The
    JSON is what gets patched, never the rendered markdown: the render is
    derived, so patching it would be overwritten by the next render and
    would leave content_json saying something the document doesn't."""
    fields, next_id = [], 0
    for path, value in _walk_strings(data):
        if not _mentions_any(value, unmatched):
            continue
        fields.append({"id": next_id, "text": value, "path": path})
        next_id += 1
    return fields


def splice_json_fields(data, fields, rewrites):
    """Return a copy of `data` with the rewritten leaves patched in."""
    import copy
    patched = copy.deepcopy(data)
    by_id = {r["id"]: r["text"] for r in rewrites}
    for field in fields:
        if field["id"] not in by_id:
            continue
        node = patched
        for key in field["path"][:-1]:
            node = node[key]
        node[field["path"][-1]] = by_id[field["id"]]
    return patched


def source_lines(*sources):
    """Every line of the corpus sources that carries a number, deduplicated
    and capped. This is the whole of what the model is allowed to treat as
    true — built here, from the same texts the gate checks against, so
    there is no route by which the model can introduce a source of its
    own."""
    seen, lines = set(), []
    for source in sources:
        for line in (source or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            if not ng.extract_numeric_mentions(stripped):
                continue
            seen.add(stripped)
            lines.append(stripped)
            if len(lines) >= MAX_SOURCE_LINES:
                return lines
    return lines


def build_repair_prompt(spans, lines, unmatched, doc_type):
    """The whole instruction. One job, stated once: make every number
    traceable or take it out, and change nothing else."""
    what = "sentences" if doc_type == "cover_letter" else "fields"
    numbered = "\n".join(f"[{s['id']}] {s['text']}" for s in spans)
    sources = "\n".join(f"- {line}" for line in lines) or "(no numeric source lines)"
    return (
        f"These {what} from a draft contain numbers that do not appear in the "
        "candidate's source material. The numbers in question are: "
        f"{', '.join(unmatched)}.\n\n"
        f"# Flagged {what}\n\n{numbered}\n\n"
        "# Every number you are allowed to use\n\n"
        f"{sources}\n\n"
        "# What to do\n\n"
        f"Rewrite each flagged {what[:-1]} so that every number in it appears in the "
        "list above exactly as given, or so that the number is gone entirely. "
        "Prefer keeping the claim and dropping the figure over inventing a "
        "different figure.\n\n"
        "Change nothing else. Keep the same voice, the same length as closely "
        "as you can, and every non-numeric claim exactly as it stands. Do not "
        "add numbers that were not flagged. Return every span you were given, "
        "each identified by its id, via the emit_repaired_spans tool."
    )
