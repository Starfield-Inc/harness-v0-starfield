import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from harness import price_table

ROOT = Path(__file__).resolve().parent.parent
REAL_TODAY = ROOT / "data" / "prices" / "price-sheet-2026-10-22.csv"
REAL_PREVIOUS = ROOT / "data" / "prices" / "price-sheet-2026-10-20.csv"
HEADER = ("read_date,provider,model,input_usd_per_1m,output_usd_per_1m,unit,"
          "source_url,page_date_if_shown,how_read,label")


def row(date, provider, model, inp, out, label=None):
    label = label or f"published by {provider}"
    return f"{date},{provider},{model},{inp},{out},USD per 1M tokens,https://x.test,none,RAW,{label}"


MAIN = [("A", "m1"), ("B", "m2")]
TODAY = "2026-10-22"
PREV = "2026-10-20"


def csv_text(*rows, header=HEADER):
    return "\n".join([header, *rows]) + "\n"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        cfg = {"models": [{"provider": p, "model": m} for p, m in MAIN]}
        self.config = self.write("main.json", json.dumps(cfg))

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def today_rows(self):
        return [
            row(TODAY, "A", "m1", 2, 10),
            row(TODAY, "B", "m2", 0.1, 0.5),
            row(TODAY, "C", "c1", 0.075, 0.30),
            row(TODAY, "D", "d1", 1, 2),
            row(TODAY, "C", "c2", 3.00, 15.00),
        ]

    def prev_rows(self):
        return [
            row(PREV, "A", "m1", 2, 10),
            row(PREV, "B", "m2", 0.1, 0.5),
            row(PREV, "C", "c1", 0.075, 0.30),
            row(PREV, "D", "d1", 1, 2),
            row(PREV, "C", "c2", 3.00, 15.00),
        ]

    def build(self, today_rows=None, prev_rows="default", **kw):
        today = self.write("today.csv", csv_text(*(self.today_rows() if today_rows is None else today_rows)))
        prev = None
        if prev_rows == "default":
            prev_rows = self.prev_rows()
        if prev_rows is not None:
            prev = self.write("prev.csv", csv_text(*prev_rows))
        return price_table.build_table(today, prev, self.config, **kw)

    def refuses(self, fragment, **kw):
        with self.assertRaises(price_table.PriceTableError) as ctx:
            self.build(**kw)
        self.assertIn(fragment, str(ctx.exception))


class Layout(Case):
    def test_heading_names_both_dates(self):
        first = self.build().splitlines()[0]
        self.assertEqual(
            first, "# Prices read on 2026-10-22. Change is against the read on 2026-10-20.")

    def test_heading_without_previous(self):
        out = self.build(prev_rows=None)
        self.assertEqual(out.splitlines()[0], "# Prices read on 2026-10-22.")
        self.assertIn("| first read |", out)

    def test_column_header(self):
        out = self.build()
        self.assertIn(
            "| Provider | Model | Input (USD per 1M tokens) | Output (USD per 1M tokens)"
            " | Change | Label |", out)

    def test_section_order_and_main_order(self):
        out = self.build(today_rows=list(reversed(self.today_rows())))
        self.assertLess(out.index("## Main models"), out.index("## All other models"))
        main_part, other_part = out.split("## All other models")
        self.assertLess(main_part.index("| m1 |"), main_part.index("| m2 |"))
        self.assertNotIn("| c1 |", main_part)
        self.assertNotIn("| m1 |", other_part)

    def test_others_grouped_by_provider_in_first_seen_order(self):
        other = self.build().split("## All other models")[1]
        positions = [other.index(f"| {m} |") for m in ("c1", "c2", "d1")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(other.count("| C |"), 2)

    def test_provider_order_is_first_seen_in_todays_file(self):
        t = [row(TODAY, "A", "m1", 2, 10), row(TODAY, "B", "m2", 1, 1),
             row(TODAY, "Z", "z1", 1, 1), row(TODAY, "C", "c1", 1, 1)]
        other = self.build(today_rows=t).split("## All other models")[1]
        self.assertLess(other.index("| Z |"), other.index("| C |"))

    def test_other_section_says_so_when_empty(self):
        t = [row(TODAY, "A", "m1", 2, 10), row(TODAY, "B", "m2", 1, 1)]
        other = self.build(today_rows=t).split("## All other models")[1]
        self.assertIn("No models in this section.", other)

    def test_label_is_printed(self):
        self.assertIn("| published by A |", self.build())

    def test_deterministic(self):
        self.assertEqual(self.build().encode(), self.build().encode())

    def test_ends_with_one_newline(self):
        out = self.build()
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))


class PriceFormat(unittest.TestCase):
    def test_rule(self):
        f = price_table.format_price
        self.assertEqual(f("10"), "$10")
        self.assertEqual(f("10.00"), "$10")
        self.assertEqual(f("2"), "$2")
        self.assertEqual(f("0.10"), "$0.10")
        self.assertEqual(f("0.1"), "$0.10")
        self.assertEqual(f("0.075"), "$0.075")
        self.assertEqual(f("2.5"), "$2.50")
        self.assertEqual(f("3.75"), "$3.75")
        self.assertEqual(f("0.50"), "$0.50")


class Changes(Case):
    def change_for(self, model, today, prev):
        t = [today if f",{model}," in r else r for r in self.today_rows()]
        p = [prev if f",{model}," in r else r for r in self.prev_rows()]
        out = self.build(today_rows=t, prev_rows=p)
        line = next(l for l in out.splitlines() if f"| {model} |" in l)
        return line.split("|")[5].strip()

    def test_no_change(self):
        self.assertEqual(self.change_for("c1", row(TODAY, "C", "c1", 0.075, 0.3),
                                         row(PREV, "C", "c1", 0.075, 0.30)), "no change")

    def test_output_change(self):
        self.assertEqual(self.change_for("m1", row(TODAY, "A", "m1", 2, 12),
                                         row(PREV, "A", "m1", 2, 10)), "output $10 -> $12")

    def test_input_change(self):
        self.assertEqual(self.change_for("m1", row(TODAY, "A", "m1", 3, 10),
                                         row(PREV, "A", "m1", 2, 10)), "input $2 -> $3")

    def test_both_change(self):
        self.assertEqual(self.change_for("c1", row(TODAY, "C", "c1", 0.1, 0.5),
                                         row(PREV, "C", "c1", 0.075, 0.3)),
                         "input $0.075 -> $0.10, output $0.30 -> $0.50")

    def test_new(self):
        prev = [r for r in self.prev_rows() if ",c2," not in r]
        out = self.build(prev_rows=prev)
        line = next(l for l in out.splitlines() if "| c2 |" in l)
        self.assertEqual(line.split("|")[5].strip(), "new")

    def test_same_model_name_other_provider_is_new(self):
        t = self.today_rows() + [row(TODAY, "D", "c1", 1, 1)]
        out = self.build(today_rows=t)
        line = next(l for l in out.splitlines() if "| D | c1 |" in l)
        self.assertEqual(line.split("|")[5].strip(), "new")

    def test_gone_models_listed_under_table(self):
        prev = self.prev_rows() + [row(PREV, "C", "old1", 1, 1), row(PREV, "E", "old2", 1, 1)]
        out = self.build(prev_rows=prev)
        self.assertIn("Not read today: C old1, E old2.", out)
        self.assertLess(out.index("## All other models"), out.index("Not read today"))

    def test_no_gone_line_when_nothing_gone(self):
        self.assertNotIn("Not read today", self.build())

    def test_any_earlier_date_works(self):
        prev = [r.replace(PREV, "2026-09-01") for r in self.prev_rows()]
        self.assertIn("against the read on 2026-09-01.", self.build(prev_rows=prev).splitlines()[0])


class StepRows(Case):
    def test_step_row_is_a_note_not_a_model(self):
        t = self.today_rows() + [row(TODAY, "A", "m1 from 2026-12-01", 4, 20)]
        out = self.build(today_rows=t)
        self.assertNotIn("| m1 from", out)
        main_part, other_part = out.split("## All other models")
        self.assertIn("A m1 from 2026-12-01: input $4, output $20.", main_part)
        self.assertNotIn("from 2026-12-01", other_part)

    def test_step_row_not_reported_as_gone(self):
        prev = self.prev_rows() + [row(PREV, "A", "m1 from 2026-11-01", 3, 15)]
        self.assertNotIn("Not read today", self.build(prev_rows=prev))

    def test_step_row_without_base_model_refused(self):
        t = self.today_rows() + [row(TODAY, "A", "zzz from 2026-12-01", 4, 20)]
        self.refuses("zzz from 2026-12-01", today_rows=t)


class Refusals(Case):
    def test_two_dates_in_today(self):
        t = self.today_rows()
        t[0] = t[0].replace(TODAY, "2026-10-21", 1)
        self.refuses("one date", today_rows=t)

    def test_timestamps_same_day_pass(self):
        t = [r.replace(TODAY, TODAY + "T10:00:00Z", 1) for r in self.today_rows()]
        t[1] = t[1].replace(TODAY + "T10:00:00Z", TODAY + "T11:30:00Z", 1)
        self.assertIn("Prices read on 2026-10-22.", self.build(today_rows=t))

    def test_two_dates_in_previous(self):
        p = self.prev_rows()
        p[0] = p[0].replace(PREV, "2026-10-19", 1)
        self.refuses("one date", prev_rows=p)

    def test_previous_not_earlier(self):
        for date in (TODAY, "2026-10-23"):
            p = [r.replace(PREV, date) for r in self.prev_rows()]
            self.refuses("earlier", prev_rows=p)

    def test_header_mismatch(self):
        for header in (HEADER.replace("unit,", ""), HEADER + ",extra",
                       HEADER.replace("provider,model", "model,provider")):
            today = self.write("t.csv", csv_text(*self.today_rows(), header=header))
            with self.assertRaises(price_table.PriceTableError) as ctx:
                price_table.build_table(today, None, self.config)
            self.assertIn("header", str(ctx.exception))

    def test_bad_labels(self):
        for label in ("", "published by ", "Published by A", "measured by someone",
                      "measured by Starfield now"):
            t = self.today_rows()
            t[2] = row(TODAY, "C", "c1", 1, 1, label=f'"{label}"')
            self.refuses("label", today_rows=t)

    def test_good_labels(self):
        t = self.today_rows()
        t[2] = row(TODAY, "C", "c1", 1, 1, label="measured by Starfield")
        self.assertIn("| measured by Starfield |", self.build(today_rows=t))

    def test_bad_prices(self):
        for bad in ("0", "-1", "abc", "", "nan", "inf"):
            for col in ("in", "out"):
                t = self.today_rows()
                t[2] = row(TODAY, "C", "c1", bad if col == "in" else 1,
                           bad if col == "out" else 1)
                self.refuses("price", today_rows=t)

    def test_bad_price_in_previous(self):
        p = self.prev_rows()
        p[2] = row(PREV, "C", "c1", -1, 1)
        self.refuses("price", prev_rows=p)

    def test_main_model_missing(self):
        t = [r for r in self.today_rows() if ",m2," not in r]
        self.refuses("B m2", today_rows=t)

    def test_duplicate_pair(self):
        t = self.today_rows() + [row(TODAY, "C", "c1", 9, 9)]
        self.refuses("twice", today_rows=t)

    def test_same_model_two_providers_is_fine(self):
        t = self.today_rows() + [row(TODAY, "D", "c1", 9, 9)]
        self.assertIn("| D | c1 |", self.build(today_rows=t))

    def test_duplicate_in_previous(self):
        p = self.prev_rows() + [row(PREV, "C", "c1", 9, 9)]
        self.refuses("twice", prev_rows=p)

    def test_empty_file(self):
        self.refuses("no rows", today_rows=[])


class Cli(Case):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = price_table.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_writes_and_prints(self):
        today = self.write("t.csv", csv_text(*self.today_rows()))
        prev = self.write("p.csv", csv_text(*self.prev_rows()))
        out_path = self.dir / "out.md"
        code, out, _ = self.run_cli("--today", str(today), "--previous", str(prev),
                                    "--out", str(out_path), "--main-models", str(self.config))
        self.assertEqual(code, 0)
        self.assertEqual(out_path.read_text(encoding="utf-8"), out)

    def test_refusal_writes_nothing(self):
        t = self.today_rows() + [row(TODAY, "C", "c1", 9, 9)]
        today = self.write("t.csv", csv_text(*t))
        out_path = self.dir / "out.md"
        code, out, err = self.run_cli("--today", str(today), "--out", str(out_path),
                                      "--main-models", str(self.config))
        self.assertNotEqual(code, 0)
        self.assertFalse(out_path.exists())
        self.assertIn("twice", err)
        self.assertEqual(out, "")


class RealFiles(unittest.TestCase):
    def test_real_pair(self):
        out = price_table.build_table(REAL_TODAY, REAL_PREVIOUS)
        main_part, other_part = out.split("## All other models")
        order = ["grok-4.7", "Claude Sonnet 5.5", "gpt-6.1-sol", "gemini-4-argon (Standard) |"]
        positions = [main_part.index(f"| {m}") for m in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("from 2026-12-01", main_part)
        self.assertNotIn("Claude Opus 5.5", main_part)

    def test_example_file_is_current(self):
        example = ROOT / "docs" / "examples" / "price-table-2026-10-22.md"
        expected = price_table.build_table(REAL_TODAY, REAL_PREVIOUS)
        self.assertEqual(example.read_text(encoding="utf-8"), expected)


if __name__ == "__main__":
    unittest.main()
