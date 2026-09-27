"""PolicyShock replay engine.

All arithmetic lives here. The LLM (SERV) never computes a rupee amount: it only
parses policy text into the rule JSON and explains the numbers this engine produces.
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

CENT = Decimal("0.01")
REQUIRED_COLUMNS = ["transaction_id", "date", "product", "category", "amount", "payment_method"]

AFFECTED, NOT_AFFECTED, UNSUPPORTED = "AFFECTED", "NOT_AFFECTED", "UNSUPPORTED"


# ----------------------------------------------------------------------------- helpers
def D(x) -> Decimal:
    return Decimal(str(x))


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def inr(x) -> str:
    """Format with Indian digit grouping, e.g. 184260 -> 'Rs 1,84,260'."""
    x = D(x)
    neg = x < 0
    x = abs(x)
    whole, frac = divmod(money(x), 1)
    s = str(int(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    paise = f"{int(frac * 100):02d}"
    out = f"Rs {s}" + (f".{paise}" if paise != "00" else "")
    return f"-{out}" if neg else out


# ----------------------------------------------------------------------------- loading
def load_rules(path) -> dict:
    rules = json.loads(Path(path).read_text())
    validate_rules(rules)
    return rules


REQUIRED_PARAMS = {
    "upi_payment_methods": list,
    "threshold_inr": (int, float),
    "standard_rate": (int, float),
    "cap_inr": (int, float),
    "cap_applies_from_inr": (int, float),
    "small_merchant_monthly_qr_limit_inr": (int, float),
    "flat_fee_inr": (int, float),
    "flat_fee_sectors": list,
    "unsupported_sectors": list,
    "autopay_payment_methods": list,
    "credit_linked_payment_methods": list,
    "capital_market_rate": (int, float),
    "capital_market_cap_inr": (int, float),
    "capital_market_sectors": list,
}


def validate_rules(rules: dict) -> None:
    if "parameters" not in rules or "rules" not in rules:
        raise ValueError("rule spec needs 'parameters' and 'rules'")
    for key, typ in REQUIRED_PARAMS.items():
        if key not in rules["parameters"]:
            raise ValueError(f"missing parameter: {key}")
        if not isinstance(rules["parameters"][key], typ):
            raise ValueError(f"parameter {key} has wrong type")


def load_merchant(path) -> dict:
    m = json.loads(Path(path).read_text())
    m.setdefault("sector", "general_retail")
    m.setdefault("qr_settles_directly_to_account", True)
    return m


def load_transactions(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["amount"] = pd.to_numeric(df["amount"], errors="raise")
    if (df["amount"] < 0).any():
        raise ValueError("negative amounts found; remove refunds or handle them before analysis")
    df["payment_method"] = df["payment_method"].astype(str).str.strip().str.upper()
    return df


# ----------------------------------------------------------------------------- core
def monthly_qr_receipts(df: pd.DataFrame, upi_qr: str = "UPI_QR") -> dict[str, Decimal]:
    qr = df[df["payment_method"] == upi_qr]
    out: dict[str, Decimal] = {}
    for month, grp in qr.groupby(qr["date"].dt.strftime("%Y-%m")):
        out[month] = sum((D(a) for a in grp["amount"]), Decimal(0))
    return out


def small_merchant_months(df: pd.DataFrame, params: dict, direct_settlement: bool) -> set:
    """Which months the merchant qualifies as exempt P2PM.

    Per NPCI FAQ Q29: migration to standard P2M happens only once inward UPI QR
    receipts exceed the monthly limit for N CONSECUTIVE months, and is permanent
    from that point on - a single over-limit month does not remove the exemption.
    """
    if not direct_settlement:
        return set()
    limit = D(params["small_merchant_monthly_qr_limit_inr"])
    need = int(params.get("small_merchant_migration_consecutive_months", 3))
    receipts = monthly_qr_receipts(df)
    months = sorted(df["date"].dt.strftime("%Y-%m").unique())

    exempt_months, consecutive_over, migrated = set(), 0, False
    for m in months:
        if migrated:
            continue  # not exempt; stays off the exempt set for this and all later months
        over = receipts.get(m, Decimal(0)) > limit
        consecutive_over = consecutive_over + 1 if over else 0
        if consecutive_over >= need:
            migrated = True  # this month and all following months are non-exempt
            continue
        exempt_months.add(m)
    return exempt_months


def evaluate(amount, method: str, month: str, merchant: dict, params: dict, small_months: set) -> dict:
    """Decide one transaction. Returns status, mdr, rule_id, reason, calc."""
    amt = D(amount)
    p = params
    threshold, rate = D(p["threshold_inr"]), D(p["standard_rate"])
    cap, cap_from = D(p["cap_inr"]), D(p["cap_applies_from_inr"])

    if method in p["autopay_payment_methods"]:
        return _r(NOT_AFFECTED, 0, "R-00",
                  "UPI Autopay / recurring mandate - no prescribed MDR regardless of amount or sector",
                  "n/a")

    if method in p["credit_linked_payment_methods"]:
        return _r(NOT_AFFECTED, 0, "R-11",
                  "Credit-linked UPI (RuPay credit card / credit line) is governed by separate "
                  "card rules, not this MDR framework", "n/a")

    if method not in p["upi_payment_methods"]:
        return _r(NOT_AFFECTED, 0, "R-01", f"{method.title()} payment is outside the UPI policy", "n/a")

    if amt <= threshold:
        return _r(NOT_AFFECTED, 0, "R-02",
                  f"{inr(amt)} is at or below the {inr(threshold)} threshold", "n/a")

    if month in small_months:
        return _r(NOT_AFFECTED, 0, "R-03",
                  f"Small-merchant (P2PM) exemption: {month} is before any 3-consecutive-month "
                  f"streak above {inr(p['small_merchant_monthly_qr_limit_inr'])} in UPI QR receipts",
                  "n/a")

    sector = merchant["sector"]

    if sector in p["capital_market_sectors"]:
        cm_rate, cm_cap = D(p["capital_market_rate"]), D(p["capital_market_cap_inr"])
        return _percent_with_cap(amt, cm_rate, cm_cap, "R-09", "R-10",
                                 "Capital-markets rate on a one-time payment (Autopay SIPs are exempt under R-00)")

    if sector in p["unsupported_sectors"]:
        return _r(UNSUPPORTED, 0, "R-07",
                  f"Sector '{sector}' has a special rate this spec does not model", "n/a")

    if sector in p["flat_fee_sectors"]:
        fee = D(p["flat_fee_inr"])
        return _r(AFFECTED, fee, "R-04",
                  f"Sector '{sector}' pays a flat fee above {inr(threshold)}", f"flat {inr(fee)}")

    return _percent_with_cap(amt, rate, cap, "R-05", "R-06", "Standard MDR on eligible UPI payment above threshold",
                             cap_from=cap_from)


def _percent_with_cap(amt: Decimal, rate: Decimal, cap: Decimal, percent_rule: str, cap_rule: str,
                      percent_label: str, cap_from: Decimal | None = None) -> dict:
    """Shared percent-then-cap arithmetic.

    cap_from, when given, is an explicit amount threshold the policy states (e.g. FAQ says
    "75,000 and above" for the standard rate) - used verbatim so the label matches the policy's
    own wording rather than a derived number. When cap_from is None (no stated threshold, as for
    capital-markets), the cap is treated as binding once raw MDR would exceed it."""
    raw = money(amt * rate)
    fee = min(raw, cap)
    capped = (amt >= cap_from) if cap_from is not None else (raw > cap)
    if capped:
        return _r(AFFECTED, fee, cap_rule,
                  f"{inr(amt)} at {rate * 100:g}% would be {inr(raw)}, capped at {inr(cap)}",
                  f"min({rate * 100:g}% x {inr(amt)} = {inr(raw)}, cap {inr(cap)}) = {inr(fee)}")
    return _r(AFFECTED, raw, percent_rule, percent_label, f"{rate * 100:g}% x {inr(amt)} = {inr(raw)}")


def _r(status, mdr, rule_id, reason, calc) -> dict:
    return {"status": status, "mdr": money(D(mdr)), "rule_id": rule_id, "reason": reason, "calc": calc}


def replay(df: pd.DataFrame, rules: dict, merchant: dict, mode: str = "actual") -> pd.DataFrame:
    """Apply the rule spec to every transaction. Returns one decision row per transaction.

    mode="actual" (the default - respects the calendar): a transaction dated before
    rules['effective_date'] gets R-EFFECTIVE-DATE (not_affected) regardless of anything else -
    correctly reflects that no MDR was actually charged before the rule existed. For any
    merchant using this tool before 15 Oct 2026, EVERY historical transaction predates the
    rule, so this mode alone always shows zero impact - it answers "has this cost me anything
    yet", not "what will this cost me". This is the safe default: nothing pretends the rule is
    already live unless explicitly asked to.

    mode="projected": the effective-date gate is skipped entirely, so historical transaction
    patterns are used as a stand-in for what volume will look like once the rule is live. This
    is the tool's main value proposition and what the dashboard's headline number uses - but the
    caller (webapp/app.py) must explicitly request it rather than getting it by default."""
    if mode not in ("actual", "projected"):
        raise ValueError("mode must be 'actual' or 'projected'")
    params = rules["parameters"]
    effective_date = rules.get("effective_date")
    small_months = small_merchant_months(
        df, params, merchant.get("qr_settles_directly_to_account", True))

    rows = []
    months = df["date"].dt.strftime("%Y-%m").tolist()
    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    for rec, month, date_str in zip(df.to_dict("records"), months, dates):
        if mode == "actual" and effective_date and date_str < effective_date:
            d = _r(NOT_AFFECTED, 0, "R-EFFECTIVE-DATE",
                  f"Policy takes effect {effective_date}; this transaction is dated {date_str}, "
                  f"before that, so no MDR was actually charged on it", "n/a")
        else:
            d = evaluate(rec["amount"], rec["payment_method"], month, merchant, params, small_months)
        rows.append({**rec, "month": month, **d})
    out = pd.DataFrame(rows)
    out["mdr"] = out["mdr"].astype(float)
    return out


# ----------------------------------------------------------------------------- outputs
def summarize(res: pd.DataFrame, rules: dict, mode: str = "actual") -> dict:
    days = max((res["date"].max() - res["date"].min()).days + 1, 1)
    annual = 365 / days
    aff = res[res["status"] == AFFECTED]
    total = round(float(aff["mdr"].sum()), 2)

    by_rule = (
        res.groupby("rule_id")
        .agg(transactions=("transaction_id", "count"), mdr=("mdr", "sum"), sales_value=("amount", "sum"))
        .round(2)
        .reset_index()
        .to_dict("records")
    )
    rule_names = {r["id"]: r["name"] for r in rules["rules"]}
    rule_outcomes = {r["id"]: r["outcome"] for r in rules["rules"]}
    for row in by_rule:
        row["rule_name"] = rule_names.get(row["rule_id"], "")
        row["outcome"] = rule_outcomes.get(row["rule_id"], "")
    by_rule.sort(key=lambda r: (r["outcome"] == "not_affected", r["rule_id"]))

    by_category = (
        aff.groupby("category")
        .agg(affected_txns=("transaction_id", "count"), mdr=("mdr", "sum"), sales_value=("amount", "sum"))
        .sort_values("mdr", ascending=False).round(2).reset_index().to_dict("records")
    )
    top_products = (
        aff.groupby("product")
        .agg(affected_txns=("transaction_id", "count"), mdr=("mdr", "sum"), sales_value=("amount", "sum"))
        .sort_values("mdr", ascending=False).head(10).round(2).reset_index().to_dict("records")
    )
    not_aff_reasons = (
        res[res["status"] == NOT_AFFECTED].groupby("rule_id")
        .agg(transactions=("transaction_id", "count"))
        .reset_index().to_dict("records")
    )
    monthly = (
        res.groupby("month")
        .agg(transactions=("transaction_id", "count"),
             affected=("status", lambda s: int((s == AFFECTED).sum())),
             mdr=("mdr", "sum"))
        .round(2).reset_index().to_dict("records")
    )
    effective_date = rules.get("effective_date", "")
    period_end_str = res["date"].max().strftime("%Y-%m-%d")
    if mode == "projected":
        period_metric_label = "Projected MDR exposure once the rule is active"
        timing_note = (f"This is a PROJECTION: it assumes the {effective_date} UPI MDR rule "
                       f"already applied to this transaction history, to show what a similar "
                       f"pattern will cost once the rule is live. It is not money that was "
                       f"actually charged.")
        annualization_note = (f"period total scaled to 365 days; projects forward assuming "
                              f"similar volume continues once the {effective_date} rule takes effect")
    else:
        period_metric_label = "Actual MDR charged as of today"
        timing_note = (f"This respects the calendar: any transaction dated before "
                       f"{effective_date} shows zero MDR (R-EFFECTIVE-DATE), because the rule "
                       f"did not exist yet. If every transaction in your file predates "
                       f"{effective_date}, this number will correctly be Rs 0 - see the "
                       f"Projected figures for what to expect once the rule takes effect.")
        annualization_note = "period total scaled to 365 days; assumes similar volume continues"

    # Business-first stats (feedback: lead with these, not the technical rule table)
    pct_affected = round(100 * len(aff) / len(res), 1) if len(res) else 0.0
    avg_mdr_per_affected = round(total / len(aff), 2) if len(aff) else 0.0
    top_category = by_category[0] if by_category else None
    top_product = top_products[0] if top_products else None
    exempt_count = int((res["status"] == NOT_AFFECTED).sum())
    pct_exempt = round(100 * exempt_count / len(res), 1) if len(res) else 0.0
    top_exempt_reason = None
    if not_aff_reasons:
        top_row = max(not_aff_reasons, key=lambda r: r["transactions"])
        top_exempt_reason = {"rule_id": top_row["rule_id"],
                             "rule_name": rule_names.get(top_row["rule_id"], top_row["rule_id"]),
                             "transactions": top_row["transactions"]}

    return {
        "policy_id": rules["policy_id"],
        "verification_status": rules.get("verification_status"),
        "mode": mode,
        "period_start": res["date"].min().strftime("%Y-%m-%d"),
        "period_end": period_end_str,
        "days_covered": days,
        "transactions_total": int(len(res)),
        "transactions_upi": int(res["payment_method"].isin(
            rules["parameters"]["upi_payment_methods"] + rules["parameters"]["autopay_payment_methods"]).sum()),
        "transactions_affected": int(len(aff)),
        "transactions_unsupported": int((res["status"] == UNSUPPORTED).sum()),
        "exempt_count": exempt_count,
        "pct_affected": pct_affected,
        "pct_exempt": pct_exempt,
        "avg_mdr_per_affected": avg_mdr_per_affected,
        "top_category": top_category,
        "top_product": top_product,
        "top_exempt_reason": top_exempt_reason,
        "sales_value_in_affected_txns": round(float(aff["amount"].sum()), 2),
        "estimated_mdr_in_period": total,
        "estimated_mdr_annualized": round(total * annual, 2),
        "annualization_note": annualization_note,
        "period_metric_label": period_metric_label,
        "is_simulation": mode == "projected",
        "timing_note": timing_note,
        "by_rule": by_rule,
        "not_affected_by_rule": not_aff_reasons,
        "by_category": by_category,
        "top_products": top_products,
        "monthly": monthly,
        "label": "estimated exposure (allocation between merchant and payment providers depends on contracts)",
    }


def impact_summary(s: dict) -> str:
    """A short, deterministic (no LLM call) plain-English paragraph for the top of the
    dashboard - answers "what does this mean for my business" before any technical table."""
    verb = "could affect" if s["mode"] == "projected" else "has affected"
    parts = [
        f"Your uploaded transaction data suggests the UPI MDR policy {verb} "
        f"{s['transactions_affected']:,} of {s['transactions_total']:,} transactions "
        f"({s['pct_affected']:g}%), for an estimated exposure of {inr(s['estimated_mdr_in_period'])} "
        f"over this period (projected to {inr(s['estimated_mdr_annualized'])}/year)."
    ]
    if s["pct_exempt"] > 0:
        reason = s["top_exempt_reason"]
        reason_txt = f", mostly because {reason['rule_name'].lower()}" if reason else ""
        parts.append(f"{s['pct_exempt']:g}% of transactions are exempt{reason_txt}.")
    if s["top_category"]:
        tc = s["top_category"]
        parts.append(f"The most affected category is {tc['category']} at {inr(tc['mdr'])}.")
    if s["transactions_affected"]:
        parts.append(f"Average MDR on an affected transaction: {inr(s['avg_mdr_per_affected'])}.")
    return " ".join(parts)


def trace(res: pd.DataFrame, rules: dict, transaction_id) -> dict:
    row = res[res["transaction_id"].astype(str) == str(transaction_id)]
    if row.empty:
        raise KeyError(f"transaction {transaction_id} not found")
    r = row.iloc[0].to_dict()
    r["date"] = r["date"].strftime("%Y-%m-%d")
    rule = next((x for x in rules["rules"] if x["id"] == r["rule_id"]), None)
    return {"transaction": r, "rule": rule, "policy_id": rules["policy_id"]}


def evidence_for_explainer(summary: dict, res: pd.DataFrame, n_examples: int = 3) -> dict:
    """Compact, aggregated payload for SERV. Never the raw CSV."""
    aff = res[res["status"] == AFFECTED]
    examples = []
    for rid, grp in aff.groupby("rule_id"):
        for _, r in grp.nlargest(n_examples, "mdr").iterrows():
            examples.append({"rule_id": rid, "amount": float(r["amount"]), "mdr": float(r["mdr"]),
                             "category": r["category"], "calc": r["calc"]})
    keep = ["policy_id", "verification_status", "mode", "period_start", "period_end", "transactions_total",
            "transactions_upi", "transactions_affected", "sales_value_in_affected_txns",
            "estimated_mdr_in_period", "estimated_mdr_annualized", "annualization_note",
            "period_metric_label", "is_simulation", "timing_note", "pct_affected", "pct_exempt",
            "avg_mdr_per_affected", "top_category", "top_exempt_reason",
            "by_rule", "not_affected_by_rule", "by_category", "label"]
    return {**{k: summary[k] for k in keep}, "top_products": summary["top_products"][:5],
            "example_transactions": examples}
