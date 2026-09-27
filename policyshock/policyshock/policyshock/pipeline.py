"""Glue between SERV and the deterministic engine.

extract_and_verify_rules: SERV proposes rule parameters from policy text; diffed against the
    hand-verified reference. The engine is only ever run on the verified reference, never on
    SERV's raw output - the diff is shown to the user as a transparency step, not used live.
explain_results: builds the evidence payload, calls SERV, verifies its numbers.
"""
from __future__ import annotations

import json
import re

from .prompts import (EXTRACT_SYSTEM, extract_user_prompt, EXPLAIN_SYSTEM, explain_user_prompt,
                      MAP_COLUMNS_SYSTEM, map_columns_user_prompt)
from .rulecheck import diff_rules, check_explanation_numbers, policy_constants
from .engine import evidence_for_explainer, REQUIRED_COLUMNS
from .serv_client import chat

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json_reply(text: str) -> dict:
    return json.loads(_FENCE.sub("", text.strip()))


def extract_and_verify_rules(policy_text: str, reference_rules: dict) -> dict:
    """Returns {'extracted': ..., 'diffs': [...], 'matches_reference': bool, 'raw': str}."""
    reply = chat(EXTRACT_SYSTEM, extract_user_prompt(policy_text, reference_rules["parameters"]),
                max_tokens=700)
    try:
        extracted = _parse_json_reply(reply["text"])
    except (json.JSONDecodeError, TypeError):
        return {"extracted": None, "diffs": None, "matches_reference": False,
               "raw": reply["text"], "error": "could not parse JSON from SERV reply"}
    diffs = diff_rules(reference_rules, extracted)
    return {"extracted": extracted, "diffs": diffs, "matches_reference": len(diffs) == 0,
           "raw": reply["text"], "cached": reply.get("cached", False)}


def map_csv_columns(headers: list) -> dict:
    """Ask SERV to map a merchant's raw CSV headers onto our REQUIRED_COLUMNS schema.

    Returns {'mapping': {target: source_header_or_None, ...}, 'complete': bool,
    'unmapped': [...], 'raw': str}. Never trusts a mapped header that isn't actually
    in the given header list (protects against a hallucinated column name)."""
    reply = chat(MAP_COLUMNS_SYSTEM, map_columns_user_prompt(headers), max_tokens=300)
    try:
        mapping = _parse_json_reply(reply["text"])
    except (json.JSONDecodeError, TypeError):
        return {"mapping": {}, "complete": False, "unmapped": list(REQUIRED_COLUMNS),
               "raw": reply["text"], "error": "could not parse JSON from SERV reply"}

    header_set = set(headers)
    clean = {}
    for field in REQUIRED_COLUMNS:
        val = mapping.get(field)
        clean[field] = val if (val and val in header_set) else None
    unmapped = [f for f, v in clean.items() if v is None]
    return {"mapping": clean, "complete": len(unmapped) == 0, "unmapped": unmapped,
           "raw": reply["text"], "cached": reply.get("cached", False)}


def explain_results(summary: dict, res, rules: dict, n_examples: int = 3) -> dict:
    """Returns {'text': ..., 'unverified_numbers': [...], 'evidence': {...}}."""
    evidence = evidence_for_explainer(summary, res, n_examples=n_examples)
    reply = chat(EXPLAIN_SYSTEM, explain_user_prompt(evidence), max_tokens=500)
    bad = check_explanation_numbers(reply["text"], evidence, constants=policy_constants(rules["parameters"]))
    return {"text": reply["text"], "unverified_numbers": bad, "evidence": evidence,
           "cached": reply.get("cached", False)}
