"""Trust checks around the LLM.

1. diff_rules: compare SERV's extracted rule parameters with the hand-verified reference.
   The engine only ever runs on verified rules; the diff is shown to the user.
2. check_explanation_numbers: every rupee figure, count, or rate/percentage in SERV's
   explanation must be traceable to either the engine's own computed output OR a fixed policy
   constant (threshold, cap, rate) - not invented.
"""
from __future__ import annotations

import re


def diff_rules(reference: dict, candidate: dict, tol: float = 1e-9) -> list[dict]:
    """Return a list of differences between two rule specs' 'parameters' blocks."""
    ref, cand = reference["parameters"], candidate.get("parameters", {})
    diffs = []
    for key in sorted(set(ref) | set(cand)):
        a, b = ref.get(key, "<missing>"), cand.get(key, "<missing>")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            same = abs(a - b) <= tol
        elif isinstance(a, list) and isinstance(b, list):
            same = sorted(map(str, a)) == sorted(map(str, b))
        else:
            same = a == b
        if not same:
            diffs.append({"parameter": key, "reference": a, "extracted": b})
    return diffs


def policy_constants(params: dict) -> dict:
    """Build the set of numbers that are legitimately part of the POLICY ITSELF (not a
    computed result) and therefore safe for an explanation to mention on their own -
    rates as percentages, thresholds, caps, flat fees, counts. Used so 'above Rs 2,000' or
    '0.4%' in an explanation isn't wrongly flagged just because it's not a computed total."""
    rate_pct = set()
    for key in ("standard_rate", "capital_market_rate"):
        v = params.get(key)
        if isinstance(v, (int, float)):
            rate_pct.add(round(v * 100, 4))
    currency_and_counts = set()
    for key in ("threshold_inr", "cap_inr", "cap_applies_from_inr", "flat_fee_inr",
               "capital_market_cap_inr", "small_merchant_monthly_qr_limit_inr",
               "small_merchant_migration_consecutive_months"):
        v = params.get(key)
        if isinstance(v, (int, float)):
            currency_and_counts.add(round(float(v), 2))
    return {"rate_percentages": rate_pct, "currency_and_counts": currency_and_counts}


_NUM = re.compile(r"(?<![\w.])(\d[\d,]*\.?\d*)")
_PERCENT = re.compile(r"(\d[\d,]*\.?\d*)\s*%")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        s = m.replace(",", "").rstrip(".")
        if s:
            try:
                out.add(round(float(s), 2))
            except ValueError:
                pass
    return out


def _percent_numbers(text: str) -> set[float]:
    """Numbers immediately followed by '%' - kept separate because a rate like '0.4%' or
    '0.02%' is small enough that the general ignore_small cutoff would otherwise hide a
    hallucinated rate (e.g. '4%' instead of '0.4%') without ever checking it."""
    out = set()
    for m in _PERCENT.findall(text):
        s = m.replace(",", "")
        try:
            out.add(round(float(s), 4))
        except ValueError:
            pass
    return out


def _flatten(obj) -> set[float]:
    found: set[float] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            found |= _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _flatten(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        found.add(round(float(obj), 2))
    elif isinstance(obj, str):
        found |= _numbers(obj)
    return found


def check_explanation_numbers(explanation: str, evidence: dict, constants: dict | None = None,
                              ignore_small: float = 12) -> list[float]:
    """Return numbers in the explanation that aren't traceable to either the computed evidence
    or a known policy constant (rate/threshold/cap/count). Two passes:
    - percentages ('0.4%', '0.02%') are checked against constants['rate_percentages'] with NO
      small-number exemption, since a wrong rate (e.g. '4%') is itself a small number and would
      otherwise slip past the ignore_small cutoff undetected.
    - everything else is checked against the evidence's own numbers plus
      constants['currency_and_counts'] (thresholds, caps, flat fees - policy constants that are
      never computed results, so would never appear in the evidence payload on their own)."""
    constants = constants or {}
    rate_pct = constants.get("rate_percentages", set())
    currency_and_counts = constants.get("currency_and_counts", set())

    pct_found = _percent_numbers(explanation)
    bad_pct = [p for p in pct_found if not any(abs(p - r) < 1e-6 for r in rate_pct)]

    text_without_pct = _PERCENT.sub("", explanation)  # avoid double-flagging "0.4" from "0.4%"
    allowed = _flatten(evidence) | currency_and_counts
    bad_general = [n for n in _numbers(text_without_pct) if n > ignore_small and n not in allowed]

    return sorted(set(bad_pct) | set(bad_general))
