"""Tests for pipeline.py's SERV glue logic. `chat()` is mocked throughout - these test the
JSON-parsing, diffing and validation logic, not the live SERV API (see scripts/smoke_test.py
for that)."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policyshock.engine import load_rules
from policyshock import pipeline

RULES = load_rules(Path(__file__).resolve().parents[1] / "rules" / "upi_mdr_2026.json")


def fake_chat(text, cached=False):
    return lambda *a, **k: {"text": text, "usage": {"total_tokens": 42}, "model": "fake", "cached": cached}


class ExtractAndVerify(unittest.TestCase):
    def test_matching_extraction_has_no_diffs(self):
        payload = json.dumps({"parameters": RULES["parameters"]})
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.extract_and_verify_rules("irrelevant policy text", RULES)
        self.assertTrue(out["matches_reference"])
        self.assertEqual(out["diffs"], [])

    def test_wrong_rate_is_caught(self):
        params = {**RULES["parameters"], "standard_rate": 0.04}  # SERV extracted 4% instead of 0.4%
        payload = json.dumps({"parameters": params})
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.extract_and_verify_rules("irrelevant policy text", RULES)
        self.assertFalse(out["matches_reference"])
        self.assertEqual([d["parameter"] for d in out["diffs"]], ["standard_rate"])

    def test_unparseable_reply_is_handled_gracefully(self):
        with patch("policyshock.pipeline.chat", fake_chat("not json at all")):
            out = pipeline.extract_and_verify_rules("irrelevant policy text", RULES)
        self.assertFalse(out["matches_reference"])
        self.assertIn("error", out)

    def test_markdown_fenced_reply_is_still_parsed(self):
        payload = "```json\n" + json.dumps({"parameters": RULES["parameters"]}) + "\n```"
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.extract_and_verify_rules("irrelevant policy text", RULES)
        self.assertTrue(out["matches_reference"])


class MapCsvColumns(unittest.TestCase):
    def test_good_mapping_is_marked_complete(self):
        headers = ["Txn ID", "Date", "Item", "Category", "Total_INR", "Mode"]
        payload = json.dumps({"transaction_id": "Txn ID", "date": "Date", "product": "Item",
                              "category": "Category", "amount": "Total_INR", "payment_method": "Mode"})
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.map_csv_columns(headers)
        self.assertTrue(out["complete"])
        self.assertEqual(out["mapping"]["amount"], "Total_INR")
        self.assertEqual(out["unmapped"], [])

    def test_hallucinated_header_is_rejected(self):
        # SERV names a column that was never in the actual CSV - must not be trusted.
        headers = ["Txn ID", "Date", "Item", "Total_INR", "Mode"]
        payload = json.dumps({"transaction_id": "Txn ID", "date": "Date", "product": "Item",
                              "category": "Category (made up)",  # not in headers!
                              "amount": "Total_INR", "payment_method": "Mode"})
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.map_csv_columns(headers)
        self.assertIsNone(out["mapping"]["category"])
        self.assertIn("category", out["unmapped"])
        self.assertFalse(out["complete"])

    def test_missing_columns_are_reported(self):
        headers = ["Date", "Item", "Total_INR"]  # no id, category, or payment method column
        payload = json.dumps({"transaction_id": None, "date": "Date", "product": "Item",
                              "category": None, "amount": "Total_INR", "payment_method": None})
        with patch("policyshock.pipeline.chat", fake_chat(payload)):
            out = pipeline.map_csv_columns(headers)
        self.assertFalse(out["complete"])
        self.assertEqual(set(out["unmapped"]), {"transaction_id", "category", "payment_method"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
