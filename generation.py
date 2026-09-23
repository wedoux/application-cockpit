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

import cover_letter_schema
import cv_schema
import prompt_assembly as pa

load_dotenv(Path(__file__).parent / ".env")

MAX_TOKENS = 8000

# Cover letter generation mode (implementation pass 2, Task 3). "freeform" is
# the existing, live path — default on purpose: plan_then_write is new and
# unvalidated against real API output until the eval harness has run it,
# and there are live applications going out of this tool today. Flip
# config.yaml's cover_letter.mode to "plan_then_write" to opt in; both paths
# stay reachable so there's always a way back to the known-good one.
DEFAULT_COVER_LETTER_MODE = "freeform"
DEFAULT_TARGET_WORDS = 300
DEFAULT_WORD_TOLERANCE_PCT = 10

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


def _cover_letter_targets(config, category):
    cl_cfg = config.get("cover_letter") or {}
    word_targets = cl_cfg.get("word_targets") or {}
    target = word_targets.get(category, DEFAULT_TARGET_WORDS)
    tolerance_pct = cl_cfg.get("word_tolerance_pct", DEFAULT_WORD_TOLERANCE_PCT)
    return target, tolerance_pct


def _proof_point_range(config, category):
    """Category-aware proof-point count (pass 4, Task 2) — config.yaml's
    cover_letter.proof_points.<category>: {min, max}. The ablation behind
    this (see cover_letter_schema.py's module docstring) found the model
    doesn't reach for extra slots on its own even when the range allows it,
    so this isn't expected to change length by itself; it exists so a role
    that genuinely needs a third distinct, well-targeted point isn't
    structurally capped at two."""
    cl_cfg = config.get("cover_letter") or {}
    ranges = cl_cfg.get("proof_points") or {}
    r = ranges.get(category) or {}
    return (r.get("min", cover_letter_schema.DEFAULT_MIN_PROOF_POINTS),
            r.get("max", cover_letter_schema.DEFAULT_MAX_PROOF_POINTS))


def generate(role, doc_type, config=None, tailored_cv_text=None):
    """Draft one document. Returns a dict with content_md, content_json (CV
    fields, or a validated cover-letter plan under plan_then_write mode,
    else None), master_cv_text, master_cv_path, model, usage, cost.

    tailored_cv_text: the tailored CV already produced for this role, if any
    — cover_letter only, ignored for doc_type == "cv" (which has no CV yet
    to be aware of). Threaded into both cover-letter paths (freeform and
    plan_then_write) so neither independently re-derives facts the CV
    already states. The caller (app.py)
    resolves this: the current request's own CV result if "cv" is also
    being generated, else the latest stored CV for the role, else None.

    Raises pa.AssemblyError on guardrail cases and GenerationError on API
    issues."""
    config = config or pa.load_config()
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the loaded .env

    if doc_type == "cv":
        a = pa.assemble(role, doc_type, config)
        result = _generate_cv(client, a)
        result["master_cv_text"] = a["master_cv_text"]
        result["master_cv_path"] = a["master_cv_path"]
        return result

    if doc_type != "cover_letter":
        raise pa.AssemblyError(f"unknown doc_type '{doc_type}'")

    category = role.get("category")
    cv_path, master_cv_text = pa.resolve_master_cv(category, config)
    # Built once, shared by both cover-letter paths and (under
    # plan_then_write) by both the plan and write calls — identical system
    # text across all of them is what keeps the cache_control block a cache
    # hit rather than a fresh write each time.
    system = pa.build_system_prompt(config, master_cv_text, category=category)
    mode = (config.get("cover_letter") or {}).get("mode", DEFAULT_COVER_LETTER_MODE)

    if mode == "plan_then_write":
        result = _generate_cover_letter_plan_then_write(
            client, role, config, system, tailored_cv_text
        )
    else:
        user = pa.build_user_prompt(role, doc_type, config, tailored_cv_text=tailored_cv_text)
        result = _generate_text(client, {"model": config["model"], "system": system, "user": user})

    result["master_cv_text"] = master_cv_text
    result["master_cv_path"] = cv_path
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


def _write_call(client, model, system, user_content):
    """The free-prose half of plan_then_write — same call shape as
    _generate_text but returning the raw response alongside the text, since
    the caller needs resp.usage/resp.stop_reason from potentially two of
    these calls (the initial write and the length-retry) rather than one
    pre-packaged result dict."""
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )
    if resp.stop_reason == "refusal":
        raise GenerationError("model refused to generate this document")
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise GenerationError(f"empty draft (stop_reason={resp.stop_reason})")
    return resp, text


def _plan_tool_call(client, model, system, user_content, plan_tool):
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[plan_tool],
        tool_choice={"type": "tool", "name": plan_tool["name"]},
        messages=[{"role": "user", "content": user_content}],
    )


def _validate_plan(plan, tailored_cv_text, jd_text, min_points, max_points):
    """Returns (schema_errors, source_line_errors, jd_trace_errors,
    jd_pointer_errors) as four separate lists, not one combined one.
    Distinguishing all four (pass 4 adds the last two) is what lets the
    retry-reason counts answer four different questions: can the model fill
    out JSON, can it trace a CV claim to something real, can it trace a JD
    claim to something real, and can it connect the two (does a proof point
    actually answer a requirement it claims to). validate_plan_json runs
    first; the other three only run once the shape is already right,
    checking a malformed plan against the CV or JD would just produce noise
    on top of a real schema error. source_line_errors is further gated on
    there being a tailored CV to check against at all, a role with none yet
    has nothing to validate a pointer against, and verify_fidelity against
    the master CV is still the fallback safety net either way, same as it
    always was."""
    schema_errors = cover_letter_schema.validate_plan_json(plan, min_points, max_points)
    source_line_errors, jd_trace_errors, jd_pointer_errors = [], [], []
    if not schema_errors:
        if tailored_cv_text:
            source_line_errors = cover_letter_schema.validate_source_lines(plan, tailored_cv_text)
        jd_trace_errors = cover_letter_schema.validate_jd_fields(plan, jd_text)
        jd_pointer_errors = cover_letter_schema.validate_proof_point_requirements(plan)
    return schema_errors, source_line_errors, jd_trace_errors, jd_pointer_errors


def _generate_cover_letter_plan_then_write(client, role, config, system, tailored_cv_text):
    """Two-step cover letter generation: a small structured plan (forced tool
    call, validated, retried once with the validation error fed back — same
    shape as _generate_cv), then a free-prose write step conditioned on the
    validated plan, plus a length retry loop the plan's target_words field
    alone can't guarantee — a baseline scan of every stored letter found "one page"
    converging on ~500-600 words regardless of category or company, a
    stable convergence a number in a prompt doesn't move on its own.

    The retry is once, not a loop: if the write step is still over target
    after correcting once, the draft is stored anyway and flagged
    (word_count_retried / actual_words on the result) — never blocked, never
    retried indefinitely. style_gate's advisory word_count check surfaces
    the final number either way, so nothing is silently lost by not
    blocking."""
    model = config["model"]
    category = role.get("category")
    default_target, tolerance_pct = _cover_letter_targets(config, category)
    min_points, max_points = _proof_point_range(config, category)
    jd_text = role.get("jd_text") or ""
    plan_tool = cover_letter_schema.build_plan_tool(min_points, max_points)

    # --- Plan step ---
    plan_user = pa.build_cover_letter_plan_user_prompt(
        role, config, tailored_cv_text, default_target, min_points, max_points
    )
    resp = _plan_tool_call(client, model, system, plan_user, plan_tool)
    if resp.stop_reason == "refusal":
        raise GenerationError("model refused to generate this document")
    plan = _extract_tool_input(resp)
    usage = _usage_dict(resp.usage)
    cost = _cost(model, resp.usage)
    plan_retried = False
    plan_retry_reason = None  # "schema" | "jd_requirement_trace" | "jd_requirement_pointer" | "source_line" | None
    plan_retry_reasons = []  # every category that fired, not just the primary one

    def _check(p):
        if p is None:
            return ["no tool call in response"], [], [], []
        return _validate_plan(p, tailored_cv_text, jd_text, min_points, max_points)

    schema_errors, source_line_errors, jd_trace_errors, jd_pointer_errors = _check(plan)
    errors = schema_errors + source_line_errors + jd_trace_errors + jd_pointer_errors
    if errors:
        plan_retried = True
        # Priority order when more than one category fires at once: schema
        # first (nothing else is trustworthy until the shape is right), then
        # a fabricated JD trace (the bigger integrity problem), then a
        # pointer that doesn't match the plan's own list, then source_line.
        if schema_errors:
            plan_retry_reasons.append("schema")
        if jd_trace_errors:
            plan_retry_reasons.append("jd_requirement_trace")
        if jd_pointer_errors:
            plan_retry_reasons.append("jd_requirement_pointer")
        if source_line_errors:
            plan_retry_reasons.append("source_line")
        plan_retry_reason = plan_retry_reasons[0]
        retry_note = (
            "\n\n# Validation error on your previous attempt\n\n"
            "Your last emit_cover_letter_plan call failed validation:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nCall emit_cover_letter_plan again with a corrected plan that fixes every issue above."
        )
        resp = _plan_tool_call(client, model, system, plan_user + retry_note, plan_tool)
        if resp.stop_reason == "refusal":
            raise GenerationError("model refused to generate this document (plan retry)")
        plan = _extract_tool_input(resp)
        usage = _add_usage(usage, _usage_dict(resp.usage))
        cost = round(cost + _cost(model, resp.usage), 4)
        schema_errors, source_line_errors, jd_trace_errors, jd_pointer_errors = _check(plan)
        errors = schema_errors + source_line_errors + jd_trace_errors + jd_pointer_errors
        if errors:
            raise GenerationError(f"cover letter plan failed validation after retry: {'; '.join(errors)}")

    # --- Write step, with one length-correction retry ---
    write_user = pa.build_cover_letter_write_user_prompt(role, config, tailored_cv_text, plan)
    resp, text = _write_call(client, model, system, write_user)
    usage = _add_usage(usage, _usage_dict(resp.usage))
    cost = round(cost + _cost(model, resp.usage), 4)

    target_words = plan["target_words"]
    tolerance_words = target_words * tolerance_pct / 100
    actual_words = len(text.split())
    word_count_retried = False
    if actual_words > target_words + tolerance_words:
        word_count_retried = True
        length_note = (
            f"\n\n# Length correction\n\nYour previous attempt was {actual_words} words against a "
            f"target of {target_words} (+/-{tolerance_pct}%). Cut it down — aim for {target_words} "
            "words. Cut content, don't just shorten sentences; the argument should still be complete."
        )
        resp, text = _write_call(client, model, system, write_user + length_note)
        usage = _add_usage(usage, _usage_dict(resp.usage))
        cost = round(cost + _cost(model, resp.usage), 4)
        actual_words = len(text.split())

    return {
        "content_md": text,
        "content_json": plan,
        "model": model,
        "usage": usage,
        "cost_usd": cost,
        "stop_reason": resp.stop_reason,
        "truncated": resp.stop_reason == "max_tokens",
        "retried": plan_retried or word_count_retried,
        "repairs": [],
        "plan_retried": plan_retried,
        "plan_retry_reason": plan_retry_reason,
        "plan_retry_reasons": plan_retry_reasons,
        "word_count_retried": word_count_retried,
        "target_words": target_words,
        "actual_words": actual_words,
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
