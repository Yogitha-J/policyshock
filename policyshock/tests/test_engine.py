import sys, unittest, copy
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policyshock.engine import load_rules, replay, summarize, AFFECTED, NOT_AFFECTED, UNSUPPORTED
from policyshock.rulecheck import diff_rules, check_explanation_numbers

RULES = load_rules(Path(__file__).resolve().parents[1] / "rules" / "upi_mdr_2026.json")
BIG = {"sector": "general_retail", "qr_settles_directly_to_account": True}
# For tests that aren't about the exemption itself: disable the "settles directly to
# account" flag so the P2PM exemption path never triggers, regardless of amounts.
NOEXEMPT = {**BIG, "qr_settles_directly_to_account": False}


def run(rows, merchant=NOEXEMPT, pad_qr=None, date="2026-10-15", mode="actual"):
    """rows: (amount, method). Date defaults to the policy's effective date itself, so these
    tests exercise the rules they're named for rather than getting swallowed by the
    R-EFFECTIVE-DATE gate (see test_effective_date_boundary for that gate's own tests)."""
    recs = [{"transaction_id": f"T{i}", "date": date, "product": "x", "category": "c",
             "amount": a, "payment_method": m} for i, (a, m) in enumerate(rows)]
    df = pd.DataFrame(recs); df["date"] = pd.to_datetime(df["date"])
    res = replay(df, RULES, merchant, mode=mode)
    return res.set_index("transaction_id")


class Engine(unittest.TestCase):
    def test_effective_date_boundary(self):
        """Before 15 Oct -> not affected (R-EFFECTIVE-DATE); on/after -> normal evaluation."""
        recs = [
            {"transaction_id": "BEFORE", "date": "2026-10-14", "product": "x", "category": "c",
             "amount": 50000, "payment_method": "UPI_QR"},
            {"transaction_id": "ON_DATE", "date": "2026-10-15", "product": "x", "category": "c",
             "amount": 50000, "payment_method": "UPI_QR"},
        ]
        df = pd.DataFrame(recs); df["date"] = pd.to_datetime(df["date"])
        res = replay(df, RULES, NOEXEMPT, mode="actual").set_index("transaction_id")
        self.assertEqual(res.loc["BEFORE", "status"], NOT_AFFECTED)
        self.assertEqual(res.loc["BEFORE", "mdr"], 0)
        self.assertEqual(res.loc["BEFORE", "rule_id"], "R-EFFECTIVE-DATE")
        self.assertEqual(res.loc["ON_DATE", "status"], AFFECTED)
        self.assertEqual(res.loc["ON_DATE", "mdr"], 200.0)

    def test_projected_mode_ignores_effective_date(self):
        recs = [{"transaction_id": "BEFORE", "date": "2026-01-01", "product": "x", "category": "c",
                "amount": 50000, "payment_method": "UPI_QR"}]
        df = pd.DataFrame(recs); df["date"] = pd.to_datetime(df["date"])
        res = replay(df, RULES, NOEXEMPT, mode="projected").set_index("transaction_id")
        self.assertEqual(res.loc["BEFORE", "status"], AFFECTED)
        self.assertEqual(res.loc["BEFORE", "mdr"], 200.0)
        self.assertNotEqual(res.loc["BEFORE", "rule_id"], "R-EFFECTIVE-DATE")

    def test_actual_mode_is_the_default(self):
        recs = [{"transaction_id": "BEFORE", "date": "2026-01-01", "product": "x", "category": "c",
                "amount": 50000, "payment_method": "UPI_QR"}]
        df = pd.DataFrame(recs); df["date"] = pd.to_datetime(df["date"])
        res = replay(df, RULES, NOEXEMPT).set_index("transaction_id")  # no mode= passed
        self.assertEqual(res.loc["BEFORE", "rule_id"], "R-EFFECTIVE-DATE")

    def test_threshold_is_strictly_above_2000(self):
        r = run([(2000, "UPI_QR"), (2000.01, "UPI_QR")])
        self.assertEqual(r.loc["T0", "status"], NOT_AFFECTED); self.assertEqual(r.loc["T0", "rule_id"], "R-02")
        self.assertEqual(r.loc["T1", "status"], AFFECTED)

    def test_standard_rate(self):
        r = run([(20000, "UPI_APP")])
        self.assertEqual(r.loc["T0", "mdr"], 80.0); self.assertEqual(r.loc["T0", "rule_id"], "R-05")

    def test_cap_boundary(self):
        r = run([(74999, "UPI_QR"), (75000, "UPI_QR"), (500000, "UPI_QR")])
        self.assertAlmostEqual(r.loc["T0", "mdr"], 300.0, places=2)  # 0.4% of 74,999 = 299.996 -> 300.00
        self.assertEqual(r.loc["T0", "rule_id"], "R-05")
        self.assertEqual(r.loc["T1", "mdr"], 300.0); self.assertEqual(r.loc["T1", "rule_id"], "R-06")
        self.assertEqual(r.loc["T2", "mdr"], 300.0)

    def test_non_upi_unaffected(self):
        r = run([(90000, "CARD"), (90000, "CASH")])
        self.assertTrue((r.loc[["T0", "T1"], "rule_id"] == "R-01").all())

    def test_small_merchant_exempt(self):
        r = run([(5000, "UPI_QR"), (30000, "UPI_QR")], merchant=BIG)  # month QR total 35,000 <= 1 lakh
        self.assertTrue((r["rule_id"] == "R-03").all()); self.assertEqual(r["mdr"].sum(), 0)

    def test_small_merchant_exemption_needs_direct_settlement(self):
        r = run([(5000, "UPI_QR")], merchant=NOEXEMPT)
        self.assertEqual(r.loc["T0", "status"], AFFECTED)

    def test_migration_needs_three_consecutive_over_limit_months(self):
        # Jan over, Feb under (streak breaks), Mar-Apr-May over (3rd consecutive month = migration).
        # A txn in Jan and May should show different outcomes even though both months are "over".
        months = {
            "2026-01": 150000, "2026-02": 50000,
            "2026-03": 150000, "2026-04": 150000, "2026-05": 150000,
            "2026-06": 10000,  # drops back under the limit - migration should still hold
        }
        recs = []
        for i, (m, total) in enumerate(months.items()):
            recs.append({"transaction_id": f"QR{i}", "date": f"{m}-05", "product": "x", "category": "c",
                        "amount": total, "payment_method": "UPI_QR"})
            recs.append({"transaction_id": f"T{i}", "date": f"{m}-10", "product": "x", "category": "c",
                        "amount": 5000, "payment_method": "UPI_APP"})
        df = pd.DataFrame(recs); df["date"] = pd.to_datetime(df["date"])
        # mode="projected": this test is about migration-streak logic, not date-effectiveness -
        # bypass the R-EFFECTIVE-DATE gate so these (deliberately pre-Oct-2026) dates aren't
        # all swallowed by it before reaching the logic under test.
        res = replay(df, RULES, BIG, mode="projected").set_index("transaction_id")
        self.assertEqual(res.loc["T0", "rule_id"], "R-03")   # Jan: streak=1, still exempt
        self.assertEqual(res.loc["T1", "rule_id"], "R-03")   # Feb: under limit, exempt, streak resets
        self.assertEqual(res.loc["T2", "rule_id"], "R-03")   # Mar: streak=1, still exempt
        self.assertEqual(res.loc["T3", "rule_id"], "R-03")   # Apr: streak=2, still exempt
        self.assertEqual(res.loc["T4", "rule_id"], "R-05")   # May: streak=3 (Mar,Apr,May) -> migrated from this month on
        self.assertEqual(res.loc["T5", "rule_id"], "R-05")   # Jun: migration is permanent even though this month is back under the limit

    def test_flat_fee_sector(self):
        r = run([(9000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "fuel"})
        self.assertEqual(r.loc["T0", "mdr"], 5.0); self.assertEqual(r.loc["T0", "rule_id"], "R-04")

    def test_utilities_is_flat_fee(self):
        r = run([(9000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "utilities"})
        self.assertEqual(r.loc["T0", "mdr"], 5.0); self.assertEqual(r.loc["T0", "rule_id"], "R-04")

    def test_autopay_is_always_exempt(self):
        # Autopay is exempt regardless of amount, sector, or the small-merchant test.
        r = run([(50000, "UPI_AUTOPAY"), (2500000, "UPI_AUTOPAY")],
               merchant={**NOEXEMPT, "sector": "fuel"})
        self.assertTrue((r["rule_id"] == "R-00").all()); self.assertEqual(r["mdr"].sum(), 0)

    def test_credit_linked_upi_is_out_of_scope(self):
        r = run([(200000, "UPI_CREDIT_LINE")], merchant={**NOEXEMPT, "sector": "fuel"})
        self.assertEqual(r.loc["T0", "status"], NOT_AFFECTED); self.assertEqual(r.loc["T0", "rule_id"], "R-11")
        self.assertEqual(r.loc["T0", "mdr"], 0)

    def test_capital_markets_percent_rate(self):
        r = run([(100000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "capital_markets"})
        # 0.02% of 100,000 = 20, matches the Business Standard worked example
        self.assertEqual(r.loc["T0", "mdr"], 20.0); self.assertEqual(r.loc["T0", "rule_id"], "R-09")

    def test_capital_markets_cap(self):
        r = run([(2000000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "capital_markets"})
        # 0.02% of 20,00,000 = 400, so the 300 cap should bind
        self.assertEqual(r.loc["T0", "mdr"], 300.0); self.assertEqual(r.loc["T0", "rule_id"], "R-10")

    def test_one_time_capital_markets_payment_is_not_exempt_like_autopay(self):
        # A one-time top-up (not flagged UPI_AUTOPAY) must NOT get the R-00 exemption.
        r = run([(100000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "capital_markets"})
        self.assertNotEqual(r.loc["T0", "rule_id"], "R-00")

    def test_unsupported_sector_is_flagged_not_guessed(self):
        r = run([(9000, "UPI_APP")], merchant={**NOEXEMPT, "sector": "education"})
        self.assertEqual(r.loc["T0", "status"], UNSUPPORTED); self.assertEqual(r.loc["T0", "mdr"], 0)
        self.assertEqual(r.loc["T0", "rule_id"], "R-07")

    def test_summary_totals_match_rows(self):
        recs = run([(20000, "UPI_QR"), (1000, "UPI_QR"), (80000, "UPI_APP")]).reset_index()
        s = summarize(recs, RULES)
        self.assertAlmostEqual(s["estimated_mdr_in_period"], recs.loc[recs.status == AFFECTED, "mdr"].sum(), places=2)

    def test_transactions_upi_counts_autopay_too(self):
        recs = run([(5000, "UPI_QR"), (5000, "UPI_AUTOPAY"), (5000, "CARD")]).reset_index()
        s = summarize(recs, RULES)
        self.assertEqual(s["transactions_upi"], 2)  # QR + Autopay, not the card payment

    def test_sample_retailer_is_not_flagged_p2pm_eligible(self):
        # Guards against flagging a high-volume merchant as P2PM-eligible: the retailer sample
        # does Rs 6L-43L/month in QR receipts, far beyond any plausible small-merchant scale,
        # so qr_settles_directly_to_account must be False.
        from policyshock.engine import load_merchant
        merchant = load_merchant(Path(__file__).resolve().parents[1] / "data" / "merchant_retailer.json")
        self.assertFalse(merchant.get("qr_settles_directly_to_account", True))


class Checks(unittest.TestCase):
    def test_diff_rules_detects_wrong_rate(self):
        cand = copy.deepcopy(RULES); cand["parameters"]["standard_rate"] = 0.04
        d = diff_rules(RULES, cand)
        self.assertEqual([x["parameter"] for x in d], ["standard_rate"])
        self.assertEqual(diff_rules(RULES, RULES), [])

    def test_number_check_flags_invented_figures(self):
        ev = {"mdr": 186103.04, "txns": 2714}
        self.assertEqual(check_explanation_numbers("You pay Rs 1,86,103.04 across 2,714 payments.", ev), [])
        self.assertEqual(check_explanation_numbers("You pay Rs 2,50,000 across 2,714 payments.", ev), [250000.0])

    def test_policy_constants_allowlist_covers_thresholds_and_caps(self):
        # ₹2,000 (threshold) and ₹300 (cap) are policy constants, never computed results,
        # so they'd never appear in the evidence payload on their own - without the
        # allowlist, a correct, expected mention of them would be wrongly flagged.
        from policyshock.rulecheck import policy_constants
        consts = policy_constants(RULES["parameters"])
        ev = {"mdr": 500.0}
        text = "The rate applies above Rs 2,000, capped at Rs 300 per transaction, totalling Rs 500."
        self.assertEqual(check_explanation_numbers(text, ev, constants=consts), [])

    def test_wrong_threshold_still_caught_even_with_constants(self):
        from policyshock.rulecheck import policy_constants
        consts = policy_constants(RULES["parameters"])
        ev = {"mdr": 500.0}
        text = "The rate applies above Rs 5,000."  # wrong - real threshold is Rs 2,000
        self.assertEqual(check_explanation_numbers(text, ev, constants=consts), [5000.0])

    def test_correct_rate_percentage_is_allowed(self):
        from policyshock.rulecheck import policy_constants
        consts = policy_constants(RULES["parameters"])
        ev = {"mdr": 100.0}
        text = "A standard rate of 0.4% applies, and capital-markets payments get 0.02%."
        self.assertEqual(check_explanation_numbers(text, ev, constants=consts), [])

    def test_wrong_rate_percentage_is_caught_even_though_small(self):
        # This is the case the OLD validator missed entirely: "4%" is well under the
        # ignore_small=12 cutoff, so a hallucinated rate could previously slip through silently.
        from policyshock.rulecheck import policy_constants
        consts = policy_constants(RULES["parameters"])
        ev = {"mdr": 100.0}
        text = "A standard rate of 4% applies to eligible payments."  # wrong - should be 0.4%
        self.assertEqual(check_explanation_numbers(text, ev, constants=consts), [4.0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
