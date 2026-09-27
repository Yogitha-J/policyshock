"""PolicyShock web app.

Run: cd policyshock && python3 -m webapp.app
Then open http://127.0.0.1:5000

The engine (policyshock/engine.py) does all arithmetic. SERV (via policyshock/pipeline.py) is
only called for three things, each behind its own button/step so loading the dashboard never
spends API credits: (1) auto-mapping a CSV's raw column headers when they don't match our
schema exactly, (2) the "Explain this" plain-language brief, (3) the /verify-rules page, which
has SERV independently extract the policy's parameters from the official FAQ text and diffs
them against our hand-verified reference - this is the "SERV isn't just a wrapper" proof point.

Every analysis computes TWO runs (policyshock/engine.py replay(mode=...)):
- "actual": respects the calendar. Any transaction dated before the policy's effective date
  shows zero MDR - correct, but means this is Rs 0 for anyone using the tool before 15 Oct 2026,
  since ALL their historical data necessarily predates the rule.
- "projected": the tool's real value - ignores the effective-date gate so historical volume can
  be used as a stand-in for what the rule will cost once it's live. This is the dashboard's
  headline number; "actual" is shown as a small, honest secondary panel.
"""
from __future__ import annotations

import io
import json
import os
import uuid
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify
import pandas as pd

from policyshock.engine import (
    load_rules, load_merchant, replay, summarize, trace, impact_summary, REQUIRED_COLUMNS, inr,
)

ROOT = Path(__file__).resolve().parents[1]
SESS_DIR = ROOT / "webapp" / "sessions"
SESS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "policyshock-dev-secret")

RULES = load_rules(ROOT / "rules" / "upi_mdr_2026.json")
FAQ_TEXT = (ROOT / "rules" / "upi_mdr_faq_text.txt").read_text()
SAMPLES = {
    "retailer": ("Sample retailer", ROOT / "data" / "sample_retailer.csv", ROOT / "data" / "merchant_retailer.json"),
    "small_shop": ("Sample small shop", ROOT / "data" / "sample_small_shop.csv", ROOT / "data" / "merchant_small_shop.json"),
    "investment_platform": ("Sample investment platform", ROOT / "data" / "sample_investment_platform.csv",
                            ROOT / "data" / "merchant_investment_platform.json"),
}
SECTORS = sorted(set(RULES["parameters"]["flat_fee_sectors"]) | {"general_retail"}
                 | set(RULES["parameters"]["unsupported_sectors"])
                 | set(RULES["parameters"]["capital_market_sectors"]))


def _store(res_projected: pd.DataFrame, s_projected: dict, s_actual: dict, merchant: dict,
          mapping_note: str | None = None) -> str:
    sid = uuid.uuid4().hex[:12]
    path = SESS_DIR / f"{sid}.json"
    res2 = res_projected.copy()
    res2["date"] = res2["date"].dt.strftime("%Y-%m-%d")
    path.write_text(json.dumps({"res": res2.to_dict("records"), "s_projected": s_projected,
                                "s_actual": s_actual, "merchant": merchant,
                                "mapping_note": mapping_note}))
    return sid


def _load(sid: str):
    path = SESS_DIR / f"{sid}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    res = pd.DataFrame(data["res"])
    res["date"] = pd.to_datetime(res["date"])
    return res, data["s_projected"], data["s_actual"], data["merchant"], data.get("mapping_note")


def _fail(error: str):
    return render_template("index.html", samples=SAMPLES, sectors=SECTORS, rules=RULES, error=error)


@app.route("/")
def index():
    return render_template("index.html", samples=SAMPLES, sectors=SECTORS, rules=RULES)


@app.route("/analyze", methods=["POST"])
def analyze():
    sample_key = request.form.get("sample")
    sector = request.form.get("sector", "general_retail")
    is_p2pm_assumption = request.form.get("p2pm_eligible", "yes") == "yes"
    mapping_note = None

    if sample_key:
        _, csv_path, merchant_path = SAMPLES[sample_key]
        df = pd.read_csv(csv_path)
        merchant = json.loads(merchant_path.read_text())
    else:
        f = request.files.get("csv_file")
        if not f or not f.filename:
            return _fail("Please choose a sample or upload a CSV.")
        try:
            df = pd.read_csv(io.BytesIO(f.read()))
        except Exception as e:
            return _fail(f"Could not read that file as CSV: {e}")
        merchant = {"name": "Your business", "sector": sector,
                   "qr_settles_directly_to_account": is_p2pm_assumption,
                   "p2pm_is_user_assumption": True}

    df.columns = [c.strip() for c in df.columns]
    lower_cols = {c.lower(): c for c in df.columns}
    missing = [c for c in REQUIRED_COLUMNS if c not in lower_cols]

    if missing:
        # Exact-name match failed - ask SERV to map the raw headers onto our schema
        # rather than immediately failing. This is what makes a real Razorpay/Paytm/
        # Pine Labs export usable without the merchant renaming anything by hand.
        try:
            from policyshock.pipeline import map_csv_columns
            result = map_csv_columns(list(df.columns))
        except Exception as e:
            return _fail(f"Missing column(s) {missing} and the auto-mapping call to SERV failed "
                        f"({e}). Rename your columns to match: {', '.join(REQUIRED_COLUMNS)}.")
        if not result["complete"]:
            return _fail(f"Could not confidently map these required fields even with SERV's help: "
                        f"{', '.join(result['unmapped'])}. Your columns: {', '.join(df.columns)}. "
                        f"Please rename them to match: {', '.join(REQUIRED_COLUMNS)}.")
        rename = {v: k for k, v in result["mapping"].items()}
        df = df.rename(columns=rename)
        pairs = ", ".join(f"'{v}' -> {k}" for k, v in result["mapping"].items())
        mapping_note = f"Columns auto-mapped by SERV: {pairs}." + (" (cached)" if result.get("cached") else "")
    else:
        df = df.rename(columns={lower_cols[c]: c for c in REQUIRED_COLUMNS})

    try:
        df["date"] = pd.to_datetime(df["date"])
        df["amount"] = pd.to_numeric(df["amount"])
        df["payment_method"] = df["payment_method"].astype(str).str.strip().str.upper()
        df["transaction_id"] = df["transaction_id"].astype(str)
        df["product"] = df["product"].astype(str)
        df["category"] = df["category"].astype(str)
    except Exception as e:
        return _fail(f"Could not parse the data: {e}")

    res_projected = replay(df, RULES, merchant, mode="projected")
    s_projected = summarize(res_projected, RULES, mode="projected")
    res_actual = replay(df, RULES, merchant, mode="actual")
    s_actual = summarize(res_actual, RULES, mode="actual")

    sid = _store(res_projected, s_projected, s_actual, merchant, mapping_note)
    return redirect(url_for("dashboard", sid=sid))


@app.route("/dashboard/<sid>")
def dashboard(sid):
    loaded = _load(sid)
    if not loaded:
        return redirect(url_for("index"))
    res, s_projected, s_actual, merchant, mapping_note = loaded
    summary_text = impact_summary(s_projected)
    return render_template("dashboard.html", s=s_projected, s_actual=s_actual, merchant=merchant,
                           sid=sid, inr=inr, rules=RULES, mapping_note=mapping_note,
                           summary_text=summary_text)


@app.route("/api/trace/<sid>/<txn_id>")
def api_trace(sid, txn_id):
    loaded = _load(sid)
    if not loaded:
        return jsonify({"error": "session not found"}), 404
    res, s_projected, s_actual, merchant, _ = loaded
    try:
        t = trace(res, RULES, txn_id)
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(t)


@app.route("/api/top-transactions/<sid>/<rule_id>")
def api_top_transactions(sid, rule_id):
    loaded = _load(sid)
    if not loaded:
        return jsonify({"error": "session not found"}), 404
    res, s_projected, s_actual, merchant, _ = loaded
    sub = res[res["rule_id"] == rule_id].sort_values("mdr", ascending=False).head(15)
    out = sub[["transaction_id", "date", "product", "category", "amount", "mdr", "reason"]].copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return jsonify(out.to_dict("records"))


@app.route("/api/explain/<sid>", methods=["POST"])
def api_explain(sid):
    loaded = _load(sid)
    if not loaded:
        return jsonify({"error": "session not found"}), 404
    res, s_projected, s_actual, merchant, _ = loaded
    try:
        from policyshock.pipeline import explain_results
        out = explain_results(s_projected, res, RULES)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"SERV call failed: {e}. Check SERV_BASE_URL / SERV_API_KEY / "
                                  f"SERV_MODEL env vars and run scripts/smoke_test.py."}), 500


@app.route("/verify-rules")
def verify_rules_page():
    """Shows SERV independently extracting rule parameters from the official FAQ text and
    diffs the result against our hand-verified reference. Loads a cached result if present;
    otherwise the page explains that clicking 'Run extraction' will spend one SERV call."""
    return render_template("verify_rules.html", rules=RULES, faq_text=FAQ_TEXT, result=None)


@app.route("/api/verify-rules", methods=["POST"])
def api_verify_rules():
    try:
        from policyshock.pipeline import extract_and_verify_rules
        out = extract_and_verify_rules(FAQ_TEXT, RULES)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"SERV call failed: {e}. Check SERV_BASE_URL / SERV_API_KEY / "
                                  f"SERV_MODEL env vars and run scripts/smoke_test.py."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
