import tempfile
import unittest
from pathlib import Path

from harness.checks import FAIL, FLAG, PASS
from harness.checks.prices import check_price_sheet

ROOT = Path(__file__).resolve().parent.parent
HEADER = ("read_date,provider,model,input_usd_per_1m,output_usd_per_1m,unit,"
          "source_url,page_date_if_shown,how_read,label")


def row(date, provider="A", model="m1", inp="1", out="2", label=None, url="https://x.test"):
    label = f"published by {provider}" if label is None else label
    return f"{date},{provider},{model},{inp},{out},USD per 1M tokens,{url},none,RAW,{label}"


class SheetCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, lines, header=HEADER):
        path = self.dir / name
        path.write_text("\n".join([header] + lines) + "\n")
        return path

    def run_checks(self, today_rows, prev_rows=None, header=HEADER):
        if prev_rows is None:
            prev_rows = [row("2026-10-29"), row("2026-10-29", "B", "m2")]
        today = self.write("price-sheet-2026-10-30.csv", today_rows, header)
        prev = self.write("price-sheet-2026-10-29.csv", prev_rows)
        return {c.name: c for c in check_price_sheet(today, prev)}

    def good_today(self):
        return [row("2026-10-30"), row("2026-10-30", "B", "m2")]


class TestRules(SheetCase):
    def test_all_pass_on_a_good_sheet(self):
        result = self.run_checks(self.good_today())
        self.assertEqual(len(result), 7)
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
        self.assertEqual(len(result), 7)
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


if __name__ == "__main__":
    unittest.main()
