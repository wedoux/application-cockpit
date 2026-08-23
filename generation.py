#!/usr/bin/env python3
"""
Generation — the Claude API call (Phase 1).
============================================
Takes a role + doc_type, assembles the resident-context prompt (prompt_assembly),
calls Claude, and returns the draft as Markdown plus token/cost telemetry.

Design choices:
- The stable context (writing-rules + about-me + master CV) is cached via
  `cache_control` on the system block, so the second doc for a role — and later
  roles in the same category within the 5-minute TTL — bill the repeated ~5k
  tokens at ~10% instead of full price.
- Thinking is disabled for these drafting calls: the task is bounded writing with
  all context supplied, so we want fast, predictable output that fits max_tokens
  and nothing but the document.
- Model comes from config (Sonnet 5 for generation). Cost is logged per call.
- CV generation (Phase 3a) forces a tool call (emit_cv) instead of Markdown, so
  there's no fence-stripping and the shape is predictable. The result is
  validated against cv_schema; on failure it retries once with the validation
  error fed back, then raises — unvalidated content is never returned.
"""

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import cv_schema
import prompt_assembly as pa

load_dotenv(Path(__file__).parent / ".env")

MAX_TOKENS = 8000

# (input, output) USD per 1M tokens. Sonnet 5 is on intro pricing through
# 2026-08-31 ($2/$10), reverting to $3/$15 after — update then.
PRICING = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class GenerationError(Exception):
    pass


def _cost(model, usage):
    p_in, p_out = PRICING.get(model, (0.0, 0.0))
    fresh = usage.input_tokens
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    out = usage.output_tokens
    total = (
        fresh * p_in
        + cache_write * p_in * 1.25   # cache write premium
        + cache_read * p_in * 0.10    # cache read discount
        + out * p_out
    ) / 1_000_000
    return round(total, 4)


def _usage_dict(u):
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _add_usage(a, b):
    return {k: a[k] + b[k] for k in a}


def generate(role, doc_type, config=None):
    """Draft one document. Returns a dict with content_md, content_json (CV
    only, else None), master_cv_text, master_cv_path, model, usage, cost.
    Raises pa.AssemblyError on guardrail cases and GenerationError on API
    issues."""
    config = config or pa.load_config()
    a = pa.assemble(role, doc_type, config)
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the loaded .env

    result = _generate_cv(client, a) if doc_type == "cv" else _generate_text(client, a)
    result["master_cv_text"] = a["master_cv_text"]
    result["master_cv_path"] = a["master_cv_path"]
    return result


def _generate_text(client, a):
    resp = client.messages.create(
        model=a["model"],
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{
            "type": "text",
            "text": a["system"],
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": a["user"]}],
    )
    if resp.stop_reason == "refusal":
        raise GenerationError("model refused to generate this document")
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise GenerationError(f"empty draft (stop_reason={resp.stop_reason})")
    return {
        "content_md": text,
        "content_json": None,
        "model": a["model"],
        "usage": _usage_dict(resp.usage),
        "cost_usd": _cost(a["model"], resp.usage),
        "stop_reason": resp.stop_reason,
        "truncated": resp.stop_reason == "max_tokens",
        "retried": False,
        "repairs": [],
    }


def _cv_tool_call(client, model, system, user_content):
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[cv_schema.CV_TOOL],
        tool_choice={"type": "tool", "name": cv_schema.CV_TOOL_NAME},
        messages=[{"role": "user", "content": user_content}],
    )


def _extract_tool_input(resp):
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    return block.input if block else None


def _extract_and_repair(resp):
    data = _extract_tool_input(resp)
    repairs = []
    if data is not None:
        data, repairs = cv_schema.repair_malformed_arrays(data)
    errors = cv_schema.validate_cv_json(data) if data is not None else ["no tool call in response"]
    return data, errors, repairs


def _generate_cv(client, a):
    """Force a structured emit_cv tool call (spec section 7). Two known failure
    modes — an array field returned as XML <item> tags (echoing "structured
    XML" language from about-me.md) or as JSON missing its outer brackets —
    are repaired deterministically before validation, so they don't cost a
    retry. Validated on receipt; one retry with the validation error fed back; fails loudly on
    a second failure. Never returns unvalidated content."""
    resp = _cv_tool_call(client, a["model"], a["system"], a["user"])
    if resp.stop_reason == "refusal":
        raise GenerationError("model refused to generate this document")
    data, errors, repairs = _extract_and_repair(resp)
    usage = _usage_dict(resp.usage)
    cost = _cost(a["model"], resp.usage)
    retried = False

    if errors:
        retried = True
        retry_note = (
            "\n\n# Validation error on your previous attempt\n\n"
            "Your last emit_cv call failed validation:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nCall emit_cv again with corrected JSON that fixes every issue above."
        )
        resp = _cv_tool_call(client, a["model"], a["system"], a["user"] + retry_note)
        if resp.stop_reason == "refusal":
            raise GenerationError("model refused to generate this document (retry)")
        data, errors, repairs = _extract_and_repair(resp)
        usage = _add_usage(usage, _usage_dict(resp.usage))
        cost = round(cost + _cost(a["model"], resp.usage), 4)
        if errors:
            raise GenerationError(f"CV JSON failed validation after retry: {'; '.join(errors)}")

    return {
        "content_md": cv_schema.cv_json_to_markdown(data),
        "content_json": data,
        "model": a["model"],
        "usage": usage,
        "cost_usd": cost,
        "stop_reason": resp.stop_reason,
        "truncated": resp.stop_reason == "max_tokens",
        "retried": retried,
        "repairs": repairs,
    }


if __name__ == "__main__":
    # Manual live smoke test against one role in the DB.
    import sys
    import db as dbmod

    role_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    doc_type = sys.argv[2] if len(sys.argv) > 2 else "cover_letter"
    conn = dbmod.connect()
    if role_id:
        row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM roles WHERE jd_text IS NOT NULL AND category != 'Review' LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        print("No eligible role (need jd_text and a non-Review category).")
        sys.exit(1)
    role = dict(row)
    print(f"Generating {doc_type} for [{role['company']}] {role['title']} ({role['category']})\n")
    r = generate(role, doc_type)
    if r["content_json"] is not None:
        import json
        print(json.dumps(r["content_json"], indent=2))
        print()
    print(r["content_md"])
    print(f"\n--- {r['usage']} | ${r['cost_usd']} | {r['model']} | stop={r['stop_reason']} | "
          f"retried={r['retried']} ---")
