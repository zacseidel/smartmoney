from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_financials as ff


def _row(val, start, end, form="10-K", fp="FY", filed="2025-11-01", fy=2025):
    row = {"val": val, "end": end, "form": form, "fp": fp, "filed": filed, "fy": fy}
    if start:
        row["start"] = start
    return row


def _facts(tag, rows, unit="USD", taxonomy="us-gaap"):
    return {taxonomy: {tag: {"units": {unit: rows}}}}


class AnnualSummaryTests(unittest.TestCase):
    def test_picks_full_year_10k_not_a_quarter_or_a_short_stub(self):
        facts = _facts("Revenues", [
            _row(10, "2025-01-01", "2025-03-31", form="10-Q", fp="Q1"),
            _row(40, "2025-01-01", "2025-03-31", form="10-K", fp="FY"),  # 90 days, not a year
            _row(100, "2024-01-01", "2024-12-31", filed="2025-02-01", fy=2024),
        ])
        facts["us-gaap"]["NetIncomeLoss"] = {"units": {"USD": [
            _row(7, "2024-01-01", "2024-12-31", filed="2025-02-01", fy=2024),
        ]}}
        facts["us-gaap"]["Assets"] = {"units": {"USD": [
            _row(500, None, "2024-12-31", filed="2025-02-01", fy=2024),
            _row(9, "2024-01-01", "2024-12-31", filed="2025-02-01", fy=2024),  # duration, not a balance
        ]}}
        summary = ff.annual_summary(facts)
        self.assertEqual(summary["revenues"], 100)
        self.assertEqual(summary["net_income"], 7)
        self.assertEqual(summary["assets"], 500)
        self.assertEqual(summary["fiscal_year"], 2024)
        self.assertEqual(summary["fiscal_period"], "FY")
        self.assertTrue(ff.has_statement(summary))

    def test_preferred_tag_wins_when_the_period_matches(self):
        facts = {
            "us-gaap": {
                "SalesRevenueNet": {"units": {"USD": [
                    _row(50, "2024-01-01", "2024-12-31", filed="2025-03-01"),
                ]}},
                "Revenues": {"units": {"USD": [
                    _row(80, "2024-01-01", "2024-12-31", filed="2025-02-01"),
                ]}},
            }
        }
        self.assertEqual(ff.annual_summary(facts)["revenues"], 80)

    def test_newer_period_beats_an_older_preferred_tag(self):
        facts = {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    _row(80, "2023-01-01", "2023-12-31", fy=2023, filed="2024-02-01"),
                ]}},
                "SalesRevenueNet": {"units": {"USD": [
                    _row(90, "2024-01-01", "2024-12-31", fy=2024, filed="2025-02-01"),
                ]}},
            }
        }
        summary = ff.annual_summary(facts)
        self.assertEqual(summary["revenues"], 90)
        self.assertEqual(summary["fiscal_year"], 2024)

    def test_stockholders_equity_beats_the_nci_total_for_the_same_date(self):
        # Agilent's "including NCI" concept is a stray negative number; the
        # stockholders-equity total is the line the page should show.
        facts = {"us-gaap": {
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"USD": [_row(-226_000_000, None, "2025-10-31")]}
            },
            "StockholdersEquity": {
                "units": {"USD": [_row(6_741_000_000, None, "2025-10-31")]}
            },
        }}
        self.assertEqual(ff.annual_summary(facts)["equity"], 6_741_000_000)

    def test_empty_payload_does_not_count_as_a_statement(self):
        self.assertEqual(ff.annual_summary({}), {})
        self.assertFalse(ff.has_statement({"ok": False, "cik": 1}))


class CikMapTests(unittest.TestCase):
    def test_class_share_alias_and_exact_ticker(self):
        payload = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway"},
        }
        mapped = ff.cik_by_ticker(payload)
        self.assertEqual(ff.resolve_cik("AAPL", mapped), 320193)
        self.assertEqual(ff.resolve_cik("BRKB", mapped), 1067983)
        self.assertIsNone(ff.resolve_cik("VGT", mapped))


if __name__ == "__main__":
    unittest.main()
