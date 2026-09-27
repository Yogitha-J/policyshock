"""Run the replay on a sample merchant and print the headline results."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policyshock.engine import (load_rules, load_merchant, load_transactions, replay,
                                summarize, trace, evidence_for_explainer, inr)

ROOT = Path(__file__).resolve().parents[1]
which = sys.argv[1] if len(sys.argv) > 1 else "retailer"

rules = load_rules(ROOT / "rules" / "upi_mdr_2026.json")
merchant = load_merchant(ROOT / "data" / f"merchant_{which}.json")
df = load_transactions(ROOT / "data" / f"sample_{which}.csv")
res = replay(df, rules, merchant)
s = summarize(res, rules)

print(f"== {merchant['name']} ==")
print(f"{s['transactions_affected']:,} of {s['transactions_total']:,} transactions affected "
      f"({s['transactions_upi']:,} were UPI)")
print(f"Estimated MDR in period : {inr(s['estimated_mdr_in_period'])}")
print(f"Annualized              : {inr(s['estimated_mdr_annualized'])}")
print(f"Sales value in affected : {inr(s['sales_value_in_affected_txns'])}")
print("\nBy rule:")
for r in s["by_rule"]:
    print(f"  {r['rule_id']} {r['rule_name']:<26} txns={r['transactions']:>6,}  mdr={inr(r['mdr'])}")
print("\nBy category:")
for r in s["by_category"]:
    print(f"  {r['category']:<16} txns={r['affected_txns']:>5,}  mdr={inr(r['mdr'])}")
aff = res[res["status"] == "AFFECTED"]
if len(aff):
    t = trace(res, rules, aff.nlargest(1, "amount").iloc[0]["transaction_id"])
    print("\nTrace of largest affected txn:", json.dumps(t["transaction"], default=str)[:300])
print("\nEvidence payload size (chars):", len(json.dumps(evidence_for_explainer(s, res))))
