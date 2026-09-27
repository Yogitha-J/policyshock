"""Prompt templates for the two SERV calls.

Both are built to keep SERV out of the arithmetic:
- EXTRACT: policy text -> structured rule JSON (checked against the hand-verified reference
  with rulecheck.diff_rules before the engine is ever allowed to use it).
- EXPLAIN: aggregated engine output -> plain-language explanation (checked afterwards with
  rulecheck.check_explanation_numbers so it can't invent a figure).
"""

EXTRACT_SYSTEM = """You are a policy-to-JSON extractor for a fintech compliance tool. You read
regulatory text (e.g. an FAQ document) and fill in a fixed JSON schema with values found in
that text.

CRITICAL RULES - a downstream program parses your output programmatically, so:
1. Output ONLY the JSON object. No prose before or after. No markdown code fences.
2. The schema's keys are FIXED. Copy every key name byte-for-byte exactly as given in the
   skeleton below. Do NOT rename, abbreviate, merge, split, or invent keys of your own -
   even if you think your name is clearer. A downstream exact-match check will otherwise
   treat a correct fact under the wrong key name as if you never extracted it at all.
3. Numbers are plain JSON numbers: 0.004 not "0.4%" or "0.4 percent"; 300 not "Rs 300" or
   "Rs 300 per transaction"; 2000 not "Rs 2,000". Strip all currency symbols, commas, and
   units from numeric fields.
4. Rates are decimals (0.4% -> 0.004, 0.02% -> 0.0002), never percentages like 0.4 or 40.
5. If the text does not state a value for a field, leave it null (or [] for a list field) -
   never guess or invent a number that is not in the source text.
6. Do not perform any calculation. Only extract what the text states."""


def extract_user_prompt(policy_text: str, schema_keys: dict) -> str:
    """schema_keys: the reference rule spec's 'parameters' dict, used only to build the
    skeleton's key names and value types - never sent as the answer, just the shape to fill."""
    import json

    def placeholder(v):
        if isinstance(v, bool):
            return False
        if isinstance(v, list):
            return []
        return None

    skeleton = {"parameters": {k: placeholder(v) for k, v in schema_keys.items()}}
    return (f"Fill in this exact JSON schema from the policy text below. Keep every key name "
           f"exactly as shown; do not add, remove, or rename any key:\n\n"
           f"{json.dumps(skeleton, indent=2)}\n\n"
           f"Policy text:\n\n{policy_text.strip()}")


EXPLAIN_SYSTEM = """You are PolicyShock's explainer. You turn an already-computed regulatory
impact analysis into a short, plain-language brief for a small business owner who is not a
lawyer or accountant.

Rules:
- Use ONLY the numbers given to you in the evidence JSON. Never calculate, round differently,
  or introduce any rupee amount, percentage or count that is not already present in the evidence.
- Do not suggest adding a surcharge or passing the fee on to customers - regulation prohibits this.
- Keep it to about 150-200 words: one sentence on total impact, 2-3 sentences on why (citing rule
  IDs like R-05), which categories are most affected, and 2-3 concrete, non-pricing next actions
  (e.g. reviewing acquirer/PSP contract terms, checking margins on the most-affected categories,
  confirming which sector rate applies, preparing accounting treatment).
- Mention at least one reason transactions were NOT affected, using the not_affected_by_rule data,
  so the merchant sees this is a balanced audit, not a scare tactic.
- If the evidence includes "is_simulation": true, open by clearly stating this is a projection -
  these transactions happened before the rule's effective date, so this shows what WOULD have
  been charged, not fees that were actually collected. Never phrase it as money already spent.
- End with one line reminding them this is an estimate and allocation between merchant and
  payment provider depends on their contract."""


def explain_user_prompt(evidence: dict) -> str:
    import json
    return "Evidence (JSON):\n" + json.dumps(evidence, indent=2)


MAP_COLUMNS_SYSTEM = """You map a merchant's raw CSV column headers onto a fixed target schema
for a payments-compliance tool. Output ONLY a JSON object, nothing else - no prose, no markdown
fences. The object has one key per target field, each set to the BEST MATCHING header string
from the CSV (copied exactly as given), or null if no column in the CSV plausibly corresponds
to that field.

Target fields:
- transaction_id: a unique per-row identifier (e.g. "Txn ID", "order_id", "Reference No")
- date: the transaction date (e.g. "Date", "Txn Date", "Created At")
- product: what was purchased/paid for, or a description (e.g. "Item", "Description", "Narration")
- category: a product/merchant category if present (e.g. "Category", "SKU Type") - null if absent
- amount: the transaction value (e.g. "Amount", "Total_INR", "price", "Gross Amount")
- payment_method: how it was paid (e.g. "Mode", "Payment Type", "Channel") - null if absent

Match on meaning, not exact spelling: "tx_amount", "Total_INR" and "Amount (Rs)" all map to
amount. Never invent a header that is not in the given list. Never map two target fields to the
same source column."""


def map_columns_user_prompt(headers: list) -> str:
    import json
    return "CSV column headers:\n" + json.dumps(headers)
