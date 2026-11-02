import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from harness.checks import FAIL, FLAG, PASS
from harness.checks.prices import check_price_sheet

ROOT = Path(__file__).resolve().parent.parent
HEADER = ("read_date,provider,model,input_usd_per_1m,output_usd_per_1m,unit,"
          "source_url,page_date_if_shown,how_read,label")


CARRIED = "\"Carried forward from the 2026-10-28 read, no longer on the provider's page\""


def row(date, provider="A", model="m1", inp="1", out="2", label=None, url="https://x.test", how="RAW",
        page_date="none"):
    label = f"published by {provider}" if label is None else label
    return f"{date},{provider},{model},{inp},{out},USD per 1M tokens,{url},{page_date},{how},{label}"


class SheetCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, lines, header=HEADER):
        path = self.dir / name
        path.write_text("\n".join([header] + lines) + "\n")
        return path

    def run_checks(self, today_rows, prev_rows=None, header=HEADER, row_changes=None):
        if prev_rows is None:
            prev_rows = [row("2026-10-29"), row("2026-10-29", "B", "m2")]
        today = self.write("price-sheet-2026-10-30.csv", today_rows, header)
        prev = self.write("price-sheet-2026-10-29.csv", prev_rows)
        changes = self.dir / "row-changes.json"
        changes.write_text(json.dumps(row_changes or {}))
        return {c.name: c for c in check_price_sheet(today, prev, changes)}

    def good_today(self):
        return [row("2026-10-30"), row("2026-10-30", "B", "m2")]


class TestRules(SheetCase):
    def test_all_pass_on_a_good_sheet(self):
        result = self.run_checks(self.good_today())
        self.assertEqual(len(result), 9)
        self.assertTrue(all(c.status == PASS for c in result.values()), result)

    def test_columns(self):
        bad = HEADER.replace("unit", "units")
        self.assertEqual(self.run_checks(self.good_today(), header=bad)["columns"].status, FAIL)
        self.assertEqual(self.run_checks(self.good_today())["columns"].status, PASS)

    def test_read_date(self):
        rows = [row("2026-10-30"), row("2026-10-29", "B", "m2")]
        self.assertEqual(self.run_checks(rows)["read date"].status, FAIL)
        self.assertEqual(self.run_checks(self.good_today())["read date"].status, PASS)

    def test_row_count(self):
        c = self.run_checks([row("2026-10-30")])["row count"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("1 rows today, 2", c.detail)
        self.assertEqual(self.run_checks(self.good_today())["row count"].status, PASS)

    def test_same_rows_lists_missing_and_new(self):
        rows = [row("2026-10-30"), row("2026-10-30", "C", "m3")]
        c = self.run_checks(rows)["same rows"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("missing: [('B', 'm2')]", c.detail)
        self.assertIn("new: [('C', 'm3')]", c.detail)
        self.assertEqual(self.run_checks(self.good_today())["same rows"].status, PASS)

    def test_same_rows_counts_a_repeated_row(self):
        prev = [row("2026-10-29"), row("2026-10-29")]
        today = [row("2026-10-30"), row("2026-10-30")]
        self.assertEqual(self.run_checks(today, prev)["same rows"].status, PASS)
        self.assertEqual(self.run_checks(today[:1] + [row("2026-10-30", "B", "m2")], prev)["same rows"].status, FAIL)

    def test_listed_row_change_is_a_flag_that_names_the_note(self):
        rows = [row("2026-10-30"), row("2026-10-30", "C", "m3")]
        listed = {"2026-10-30": {"added": [["C", "m3"]], "removed": [["B", "m2"]], "note": "B renamed m2 to m3"}}
        result = self.run_checks(rows, row_changes=listed)
        c = result["same rows"]
        self.assertEqual(c.status, FLAG)
        self.assertIn("B renamed m2 to m3", c.detail)
        self.assertIn("new: [('C', 'm3')]", c.detail)
        self.assertEqual(result["row count"].status, PASS)

    def test_listed_new_row_makes_row_count_a_flag(self):
        rows = self.good_today() + [row("2026-10-30", "C", "m3")]
        listed = {"2026-10-30": {"added": [["C", "m3"]], "removed": [], "note": "C added m3"}}
        c = self.run_checks(rows, row_changes=listed)["row count"]
        self.assertEqual(c.status, FLAG)
        self.assertIn("C added m3", c.detail)

    def test_row_change_not_listed_still_fails(self):
        rows = self.good_today() + [row("2026-10-30", "C", "m3"), row("2026-10-30", "D", "m4")]
        listed = {"2026-10-30": {"added": [["C", "m3"]], "removed": [], "note": "C added m3"}}
        result = self.run_checks(rows, row_changes=listed)
        self.assertEqual(result["same rows"].status, FAIL)
        self.assertIn("new: [('D', 'm4')]", result["same rows"].detail)
        self.assertNotIn("('C', 'm3')", result["same rows"].detail)
        self.assertEqual(result["row count"].status, FAIL)

    def test_row_change_listed_for_another_date_fails(self):
        rows = self.good_today() + [row("2026-10-30", "C", "m3")]
        listed = {"2026-10-31": {"added": [["C", "m3"]], "removed": [], "note": "C added m3"}}
        self.assertEqual(self.run_checks(rows, row_changes=listed)["same rows"].status, FAIL)

    def test_unreadable_row_changes_file_fails_the_row_rules(self):
        today = self.write("price-sheet-2026-10-30.csv", self.good_today())
        prev = self.write("price-sheet-2026-10-29.csv", [row("2026-10-29"), row("2026-10-29", "B", "m2")])
        bad = self.dir / "row-changes.json"
        bad.write_text("{not json")
        result = {c.name: c for c in check_price_sheet(today, prev, bad)}
        self.assertEqual(result["same rows"].status, FAIL)
        self.assertIn("row-changes.json", result["same rows"].detail)
        self.assertEqual(result["labels"].status, PASS)

    def test_carried_rows_flag_names_each_row_and_its_date(self):
        rows = [row("2026-10-30"), row("2026-10-30", "B", "m2", how=CARRIED,
                                       label='"published by B, not on the page on 2026-10-30"')]
        result = self.run_checks(rows)
        c = result["carried rows"]
        self.assertEqual(c.status, FLAG)
        self.assertIn("('B', 'm2') carried from 2026-10-28", c.detail)
        self.assertEqual(result["labels"].status, PASS)
        self.assertEqual(self.run_checks(self.good_today())["carried rows"].status, PASS)

    def test_price_changes_ignore_a_carried_row(self):
        prev = [row("2026-10-29"), row("2026-10-29", "B", "m2", inp="0.5")]
        rows = [row("2026-10-30"), row("2026-10-30", "B", "m2", how=CARRIED)]
        c = self.run_checks(rows, prev)["price changes"]
        self.assertEqual((c.status, c.detail), (PASS, "no price changed"))

    def test_page_dates_flag_a_provider_that_lost_its_date(self):
        prev = [row("2026-10-29", page_date="2026-10-19"), row("2026-10-29", "B", "m2", page_date="2026-10-01")]
        rows = [row("2026-10-30"), row("2026-10-30", "B", "m2", page_date="2026-10-01")]
        c = self.run_checks(rows, prev)["page dates"]
        self.assertEqual(c.status, FLAG)
        self.assertEqual(c.detail, "A shows no page date today, last seen 2026-10-19")

    def test_page_dates_pass_when_dates_stay_change_or_never_existed(self):
        prev = [row("2026-10-29", page_date="2026-10-19"), row("2026-10-29", "B", "m2")]
        rows = [row("2026-10-30", page_date="2026-10-29"), row("2026-10-30", "B", "m2")]
        c = self.run_checks(rows, prev)["page dates"]
        self.assertEqual((c.status, c.detail), (PASS, "no provider lost its page date"))

    def test_page_dates_ignore_a_carried_row(self):
        prev = [row("2026-10-29", page_date="2026-10-19"), row("2026-10-29", "A", "m2", page_date="2026-10-19")]
        rows = [row("2026-10-30", page_date="2026-10-19"), row("2026-10-30", "A", "m2", how=CARRIED)]
        self.assertEqual(self.run_checks(rows, prev)["page dates"].status, PASS)

    def test_prices_present(self):
        for bad in ("", "abc", "-1"):
            rows = [row("2026-10-30", inp=bad), row("2026-10-30", "B", "m2")]
            self.assertEqual(self.run_checks(rows)["prices present"].status, FAIL, bad)
        rows = [row("2026-10-30", out=""), row("2026-10-30", "B", "m2")]
        self.assertEqual(self.run_checks(rows)["prices present"].status, FAIL)
        rows = [row("2026-10-30", inp="0", out="0.5"), row("2026-10-30", "B", "m2")]
        self.assertEqual(self.run_checks(rows)["prices present"].status, PASS)

    def test_labels(self):
        rows = [row("2026-10-30", label="from A"), row("2026-10-30", "B", "m2")]
        self.assertEqual(self.run_checks(rows)["labels"].status, FAIL)
        rows = [row("2026-10-30", url="http://x.test"), row("2026-10-30", "B", "m2")]
        self.assertEqual(self.run_checks(rows)["labels"].status, FAIL)
        self.assertEqual(self.run_checks(self.good_today())["labels"].status, PASS)

    def test_price_changes_flag(self):
        rows = [row("2026-10-30", inp="1.5"), row("2026-10-30", "B", "m2")]
        c = self.run_checks(rows)["price changes"]
        self.assertEqual(c.status, FLAG)
        self.assertIn("('A', 'm1') input_usd_per_1m 1 -> 1.5", c.detail)

    def test_same_number_written_differently_is_no_change(self):
        rows = [row("2026-10-30", inp="1.00"), row("2026-10-30", "B", "m2")]
        c = self.run_checks(rows)["price changes"]
        self.assertEqual((c.status, c.detail), (PASS, "no price changed"))


class TestBadFiles(SheetCase):
    def test_missing_today_fails_without_raising(self):
        prev = self.write("price-sheet-2026-10-29.csv", [row("2026-10-29")])
        result = check_price_sheet(self.dir / "price-sheet-2026-10-30.csv", prev)
        self.assertEqual(len(result), 9)
        self.assertTrue(all(c.status == FAIL for c in result))

    def test_missing_previous_fails_only_comparing_rules(self):
        today = self.write("price-sheet-2026-10-30.csv", [row("2026-10-30")])
        result = {c.name: c for c in check_price_sheet(today, self.dir / "none.csv")}
        self.assertEqual(result["columns"].status, PASS)
        for name in ("row count", "same rows", "price changes"):
            self.assertEqual(result[name].status, FAIL, name)

    def test_empty_file_does_not_raise(self):
        today = self.dir / "price-sheet-2026-10-30.csv"
        today.write_text("")
        prev = self.write("price-sheet-2026-10-29.csv", [row("2026-10-29")])
        result = {c.name: c for c in check_price_sheet(today, prev)}
        self.assertEqual(result["columns"].status, FAIL)


class TestRealSheets(unittest.TestCase):
    def test_30_oct_against_29_oct(self):
        d = ROOT / "data" / "prices"
        result = {c.name: c for c in check_price_sheet(
            d / "price-sheet-2026-10-30.csv", d / "price-sheet-2026-10-29.csv")}
        self.assertFalse([c for c in result.values() if c.status == FAIL], result)
        self.assertEqual(result["price changes"].status, PASS)

    def test_2_nov_mistral_change_flags_but_does_not_fail(self):
        d = ROOT / "data" / "prices"
        with tempfile.TemporaryDirectory() as tmp:
            today = Path(tmp) / "price-sheet-2026-11-02.csv"
            today.write_text(build_2_nov_sheet(d / "price-sheet-2026-10-30.csv"), encoding="utf-8")
            result = {c.name: c for c in check_price_sheet(today, d / "price-sheet-2026-10-30.csv")}
        self.assertFalse([c for c in result.values() if c.status == FAIL], result)
        self.assertEqual(result["same rows"].status, FLAG)
        self.assertIn("Mistral replaced Large 3 with Large 4", result["same rows"].detail)
        self.assertEqual(result["carried rows"].status, FLAG)
        self.assertIn("('Mistral', 'Mistral Large 3') carried from 2026-10-30", result["carried rows"].detail)
        self.assertIn("('Mistral', 'Mistral Medium 3.5') carried from 2026-10-30", result["carried rows"].detail)
        self.assertEqual(result["price changes"].status, PASS)

    def test_2_nov_google_without_page_date_flags_with_the_last_date(self):
        d = ROOT / "data" / "prices"
        with tempfile.TemporaryDirectory() as tmp:
            today = Path(tmp) / "price-sheet-2026-11-02.csv"
            text = build_2_nov_sheet(d / "price-sheet-2026-10-30.csv").replace(",2026-10-28,", ",none,")
            today.write_text(text, encoding="utf-8")
            result = {c.name: c for c in check_price_sheet(today, d / "price-sheet-2026-10-30.csv")}
        c = result["page dates"]
        self.assertEqual((c.status, c.detail), (FLAG, "Google shows no page date today, last seen 2026-10-28"))


def build_2_nov_sheet(sheet_30_oct):
    """The 30 Oct rows dated 2 Nov, with the Mistral rows the reader writes from the saved 2 Nov page."""
    from tests.test_price_reader import read_mistral
    with open(sheet_30_oct, newline="", encoding="utf-8") as f:
        rows = [dict(r, read_date="2026-11-02") for r in csv.DictReader(f) if r["provider"] != "Mistral"]
    rows += read_mistral()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
