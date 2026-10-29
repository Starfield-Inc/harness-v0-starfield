import copy
import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from harness import price_reader as pr

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "price_reader"
SHEET_1020 = ROOT / "data" / "prices" / "price-sheet-2026-10-20.csv"
COLUMNS = [
    "read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m", "unit",
    "source_url", "page_date_if_shown", "how_read", "label",
]
URL = "https://example.test/prices"
TABLE = r"\|\s*\$(?P<input>[\d.]+)\s*\|\s*\$[\d.]+\s*\|\s*\$(?P<output>[\d.]+)"
STEP_VALUE = (r"\$[\d.]+ through [A-Za-z]+ \d+, \d{4}\.\s*"
              r"\$(?P<value>[\d.]+) starting (?P<date>[A-Za-z]+ \d+, \d{4})")


def config():
    return {"pages": [{
        "provider": "Acme",
        "url": URL,
        "unit": "usd_per_1m",
        "unit_check": "(?i)per 1M tokens",
        "page_date": r"Last updated (?P<date>\d{4}-\d{2}-\d{2}) UTC",
        "models": [
            {"model": "alpha-1", "mode": "text", "anchor": "alpha-1 |", "row": TABLE},
            {"model": "gemma-9 (Standard)", "mode": "text", "anchor": "gemma-9",
             "input": r"Input price[^$]*\$(?P<value>[\d.]+)",
             "output": r"Output price[^$]*\$(?P<value>[\d.]+)",
             "step": {"input": r"Input price[^$]*" + STEP_VALUE,
                      "output": r"Output price[^$]*" + STEP_VALUE}},
            {"model": "beta-2", "mode": "html", "anchor": '{"name":"beta-2"',
             "unit": "usd_per_1m_x10000",
             "input": r'"in":"(?P<value>\d+)"', "output": r'"out":"(?P<value>\d+)"'},
        ],
    }]}


def page(name):
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


def fetcher(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    fetch.calls = calls
    return fetch


def NOW():
    return datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)


def read(cfg=None, html=None):
    fetch = fetcher({URL: html if html is not None else page("good")})
    return pr.read_prices(cfg or config(), fetch, NOW), fetch


class ParseTests(unittest.TestCase):
    def test_good_parse(self):
        rows, _ = read()
        got = [(r["model"], r["input_usd_per_1m"], r["output_usd_per_1m"]) for r in rows]
        self.assertEqual(got, [
            ("alpha-1", "1.5", "6"),
            ("gemma-9 (Standard)", "2", "10"),
            ("gemma-9 (Standard) from 2026-12-01", "4", "20"),
            ("beta-2", "2", "6"),
        ])
        first = rows[0]
        self.assertEqual(list(first), COLUMNS)
        self.assertEqual(first["read_date"], "2026-10-28")
        self.assertEqual(first["provider"], "Acme")
        self.assertEqual(first["unit"], "USD per 1M tokens")
        self.assertEqual(first["source_url"], URL)
        self.assertEqual(first["page_date_if_shown"], "2026-10-27")
        self.assertEqual(first["how_read"], "Script: page text")
        self.assertEqual(first["label"], "published by Acme")
        self.assertEqual(rows[-1]["how_read"], "Script: raw HTML")

    def test_page_without_page_date_setting_says_none(self):
        cfg = config()
        del cfg["pages"][0]["page_date"]
        rows, _ = read(cfg)
        self.assertEqual(rows[0]["page_date_if_shown"], "none")

    def test_no_step_row_when_page_shows_no_change(self):
        html = page("good")
        html = html.replace(" through November 30, 2026.\n$4.00 starting December 1, 2026.", "")
        html = html.replace(" through November 30, 2026.\n$20.00 starting December 1, 2026.", "")
        rows, _ = read(html=html)
        self.assertEqual([r["model"] for r in rows],
                         ["alpha-1", "gemma-9 (Standard)", "beta-2"])

    def test_non_breaking_space_in_page_text_reads_as_a_space(self):
        self.assertEqual(pr.page_text("<p>$0.30&nbsp;(cached)</p>"), "$0.30 (cached)")

    def test_each_page_is_fetched_once(self):
        _, fetch = read()
        self.assertEqual(fetch.calls, [URL])

    def test_missing_model_names_provider_and_model(self):
        cfg = config()
        cfg["pages"][0]["models"][0]["anchor"] = "no-such-model |"
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(cfg)
        self.assertIn("Acme", str(ctx.exception))
        self.assertIn("alpha-1", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    def test_unparsable_price_is_refused(self):
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(html=page("bad_price"))
        self.assertIn("alpha-1", str(ctx.exception))

    def test_zero_price_is_refused(self):
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(html=page("zero_price"))
        self.assertIn("positive", str(ctx.exception))

    def test_input_above_output_is_refused(self):
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(html=page("swapped"))
        self.assertIn("input", str(ctx.exception))
        self.assertIn("alpha-1", str(ctx.exception))

    def test_page_that_says_per_1k_is_refused_when_config_says_per_1m(self):
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(html=page("per_1k"))
        self.assertIn("unit", str(ctx.exception))
        self.assertIn("Acme", str(ctx.exception))

    def test_per_1k_page_is_converted_when_config_says_per_1k(self):
        cfg = config()
        cfg["pages"][0]["unit"] = "usd_per_1k"
        cfg["pages"][0]["unit_check"] = "(?i)per 1K tokens"
        cfg["pages"][0]["models"] = cfg["pages"][0]["models"][:1]
        rows, _ = read(cfg, html=page("per_1k"))
        self.assertEqual((rows[0]["input_usd_per_1m"], rows[0]["output_usd_per_1m"]), ("1.5", "6"))
        self.assertEqual(rows[0]["unit"], "USD per 1M tokens")

    def test_unknown_unit_in_config_is_refused(self):
        cfg = config()
        cfg["pages"][0]["unit"] = "usd_per_token"
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(cfg)
        self.assertIn("usd_per_token", str(ctx.exception))

    def test_step_with_different_dates_is_refused(self):
        html = page("good").replace("$20.00 starting December 1", "$20.00 starting December 2")
        with self.assertRaises(pr.PriceReadError) as ctx:
            read(html=html)
        self.assertIn("gemma-9", str(ctx.exception))

    def test_page_that_cannot_be_fetched_is_refused(self):
        fetch = fetcher({URL: pr.PriceReadError("Acme: could not fetch " + URL)})
        with self.assertRaises(pr.PriceReadError) as ctx:
            pr.read_prices(config(), fetch, NOW)
        self.assertIn("Acme", str(ctx.exception))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.cfg_path = base / "sources.json"
        self.cfg_path.write_text(json.dumps(config()), encoding="utf-8")
        self.sheets = base / "sheets"
        self.sheets.mkdir()

    def run_reader(self, pages, now=NOW):
        return pr.run(self.cfg_path, self.sheets, fetch=fetcher(pages), now=now)

    def test_writes_dated_file_with_header_and_rows(self):
        path = self.run_reader({URL: page("good")})
        self.assertEqual(path.name, "price-sheet-2026-10-28.csv")
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(reader.fieldnames, COLUMNS)
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["read_date"] for r in rows}, {"2026-10-28"})

    def test_nothing_is_written_when_a_model_is_missing(self):
        html = page("good").replace("alpha-1", "alpha-9")
        with self.assertRaises(pr.PriceReadError):
            self.run_reader({URL: html})
        self.assertEqual(list(self.sheets.iterdir()), [])

    def test_nothing_is_written_when_a_later_page_fails(self):
        cfg = config()
        second = copy.deepcopy(cfg["pages"][0])
        second["provider"] = "Other"
        second["url"] = "https://example.test/other"
        cfg["pages"].append(second)
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        pages = {URL: page("good"), second["url"]: pr.PriceReadError("Other: could not fetch")}
        with self.assertRaises(pr.PriceReadError):
            self.run_reader(pages)
        self.assertEqual(list(self.sheets.iterdir()), [])

    def test_two_read_dates_in_one_file_are_refused(self):
        rows, _ = read()
        rows[1]["read_date"] = "2026-10-29"
        with self.assertRaises(pr.PriceReadError) as ctx:
            pr.write_sheet(rows, self.sheets)
        self.assertIn("read date", str(ctx.exception))
        self.assertEqual(list(self.sheets.iterdir()), [])

    def test_run_that_crosses_midnight_utc_is_refused(self):
        times = iter([datetime(2026, 10, 28, 23, 59, 59, tzinfo=timezone.utc),
                      datetime(2026, 10, 29, 0, 0, 3, tzinfo=timezone.utc)])
        with self.assertRaises(pr.PriceReadError) as ctx:
            self.run_reader({URL: page("good")}, now=lambda: next(times))
        self.assertIn("midnight", str(ctx.exception))
        self.assertEqual(list(self.sheets.iterdir()), [])

    def test_existing_sheet_for_the_day_is_kept_and_no_request_is_made(self):
        existing = self.sheets / "price-sheet-2026-10-28.csv"
        existing.write_text("keep me", encoding="utf-8")
        fetch = fetcher({URL: page("good")})
        with self.assertRaises(pr.PriceReadError):
            pr.run(self.cfg_path, self.sheets, fetch=fetch, now=NOW)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(fetch.calls, [])


class RepoConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "prices" / "sources.json").read_text(encoding="utf-8"))

    def test_sources_cover_the_31_models_of_the_20_oct_sheet(self):
        with open(SHEET_1020, newline="", encoding="utf-8") as f:
            sheet = list(csv.DictReader(f))
        self.assertEqual(len(sheet), 31)
        want = [(r["provider"], r["model"], r["source_url"]) for r in sheet]
        got = [(p["provider"], m["model"], p["url"])
               for p in self.cfg["pages"] for m in p["models"]]
        self.assertEqual(sorted(got), sorted(want))

    def test_every_model_has_a_way_to_read_both_prices(self):
        for p in self.cfg["pages"]:
            for m in p["models"]:
                self.assertIn("anchor", m, m["model"])
                self.assertTrue("row" in m or ("input" in m and "output" in m), m["model"])


if __name__ == "__main__":
    unittest.main()
