import csv
import json
import struct
import tempfile
import unittest
from pathlib import Path

from harness import chart

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

ROOT = Path(__file__).resolve().parent.parent
SPEC_1 = ROOT / "charts" / "2026-10-20" / "001-output-price.json"
SPEC_1_SITE = ROOT / "charts" / "2026-10-20" / "001-output-price-site.json"
PRICE_CSV = ROOT / "data" / "prices" / "price-sheet-2026-10-20.csv"
ALLOWED_PRICE_COLUMNS = [
    "read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m", "unit",
    "source_url", "page_date_if_shown", "how_read", "label",
]
needs_mpl = unittest.skipUnless(HAVE_MPL, "matplotlib is not installed")


def spec(**over):
    s = {
        "id": "demo", "title": "Demo finding: B is 2x A", "data": "demo.csv",
        "date_col": "read_date", "date_text": "Prices read by hand on 20 Oct 2026",
        "value_col": "out", "value_format": "${:g}", "axis_label": "USD per 1M tokens",
        "label_col": "label", "error_col": None,
        "bars": [{"provider": "P", "model": "A", "name": "P A"},
                 {"provider": "Q", "model": "B", "name": "Q B"}],
        "note": "A note.", "links": "Method: m | Data: d | Corrections: c",
    }
    s.update(over)
    return s


def rows(over=None):
    over = over or {}
    base = [
        {"read_date": "2026-10-20", "provider": "P", "model": "A", "out": "5",
         "label": "published by P", "sd": ""},
        {"read_date": "2026-10-20", "provider": "Q", "model": "B", "out": "10",
         "label": "published by Q", "sd": ""},
    ]
    for i, r in enumerate(base):
        r.update(over.get(i, {}))
    return base


def write_csv(d, data, name="demo.csv"):
    path = Path(d) / name
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)
    return path


def bars_for(sp, data):
    with tempfile.TemporaryDirectory() as d:
        write_csv(d, data)
        return chart.load_bars(sp, Path(d))


class LoadBarsTest(unittest.TestCase):
    def test_bars_come_in_spec_order_with_value_name_and_kind(self):
        bars = bars_for(spec(), rows())
        self.assertEqual([b["name"] for b in bars], ["P A", "Q B"])
        self.assertEqual([b["value"] for b in bars], [5.0, 10.0])
        self.assertEqual([b["kind"] for b in bars], ["published", "published"])
        self.assertEqual(bars[0]["label"], "published by P")

    def test_more_than_one_read_date_is_refused_with_both_dates_named(self):
        data = rows({1: {"read_date": "2026-10-27"}})
        with self.assertRaisesRegex(chart.ChartError, r"one date.*2026-10-20.*2026-10-27"):
            bars_for(spec(), data)

    def test_extra_rows_on_another_date_are_refused_even_if_not_charted(self):
        data = rows() + [{"read_date": "2026-10-27", "provider": "Z", "model": "Z", "out": "1",
                          "label": "published by Z", "sd": ""}]
        with self.assertRaisesRegex(chart.ChartError, "one date"):
            bars_for(spec(), data)

    def test_timestamps_on_the_same_day_count_as_one_date(self):
        data = rows({0: {"read_date": "2026-10-22T10:00:00Z"},
                       1: {"read_date": "2026-10-22T11:30:00Z"}})
        self.assertEqual(len(bars_for(spec(), data)), 2)

    def test_timestamps_on_different_days_are_refused(self):
        data = rows({0: {"read_date": "2026-10-22T10:00:00Z"},
                       1: {"read_date": "2026-10-23T01:00:00Z"}})
        with self.assertRaisesRegex(chart.ChartError, "one date"):
            bars_for(spec(), data)

    def test_missing_label_is_refused_and_names_the_row(self):
        with self.assertRaisesRegex(chart.ChartError, r"label.*Q B"):
            bars_for(spec(), rows({1: {"label": ""}}))

    def test_label_must_be_published_by_or_measured_by_starfield(self):
        with self.assertRaisesRegex(chart.ChartError, "published by"):
            bars_for(spec(), rows({0: {"label": "from a blog"}}))

    def test_measured_by_starfield_is_accepted_and_has_its_own_kind(self):
        bars = bars_for(spec(), rows({0: {"label": "measured by Starfield"}}))
        self.assertEqual(bars[0]["kind"], "measured")

    def test_a_bar_missing_from_the_csv_is_refused_by_name(self):
        sp = spec(bars=[{"provider": "P", "model": "Nope", "name": "P Nope"}])
        with self.assertRaisesRegex(chart.ChartError, "Nope"):
            bars_for(sp, rows())

    def test_a_non_number_value_is_refused(self):
        with self.assertRaisesRegex(chart.ChartError, "out"):
            bars_for(spec(), rows({0: {"out": "five"}}))

    def test_error_column_gives_whisker_values(self):
        data = rows({0: {"sd": "0.5"}, 1: {"sd": "1.25"}})
        bars = bars_for(spec(error_col="sd"), data)
        self.assertEqual([b["error"] for b in bars], [0.5, 1.25])

    def test_missing_spec_key_is_refused_by_name(self):
        sp = spec()
        del sp["title"]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(sp), encoding="utf-8")
            with self.assertRaisesRegex(chart.ChartError, "title"):
                chart.load_spec(p)


class AltTextTest(unittest.TestCase):
    def test_alt_text_states_every_value_label_and_the_date(self):
        text = chart.alt_text(spec(), bars_for(spec(), rows()))
        for part in ("Demo finding: B is 2x A", "P A", "$5", "published by P",
                     "Q B", "$10", "published by Q", "20 Oct 2026"):
            self.assertIn(part, text)


@needs_mpl
class RenderTest(unittest.TestCase):
    def build(self, sp, data):
        d = tempfile.mkdtemp()
        write_csv(d, data)
        p = Path(d) / "spec.json"
        p.write_text(json.dumps(sp), encoding="utf-8")
        out = Path(d) / "out"
        return chart.build(p, out), out

    def test_build_writes_svg_png_and_alt_text(self):
        paths, out = self.build(spec(), rows())
        self.assertEqual(sorted(p.name for p in paths), ["demo.alt.txt", "demo.png", "demo.svg"])
        for p in paths:
            self.assertGreater(p.stat().st_size, 100, p.name)

    def test_png_is_a_real_png_at_least_1000_px_wide(self):
        paths, _ = self.build(spec(), rows())
        png = next(p for p in paths if p.suffix == ".png").read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png[16:24])
        self.assertGreaterEqual(width, 1000)
        self.assertGreater(width, height)

    def test_height_option_sets_the_png_height(self):
        paths, _ = self.build(dict(spec(), height=630), rows())
        png = next(p for p in paths if p.suffix == ".png").read_bytes()
        self.assertEqual(struct.unpack(">II", png[16:24]), (1200, 630))

    def test_bad_height_is_refused_with_a_clear_message(self):
        with self.assertRaises(chart.ChartError) as cm:
            self.build(dict(spec(), height="tall"), rows())
        self.assertIn("height", str(cm.exception))

    def test_svg_carries_title_names_values_labels_date_and_links_as_text(self):
        paths, _ = self.build(spec(), rows())
        svg = next(p for p in paths if p.suffix == ".svg").read_text(encoding="utf-8")
        for part in ("Demo finding: B is 2x A", "P A", "Q B", "$5", "$10",
                     "published by P", "published by Q", "Prices read by hand on 20 Oct 2026",
                     "Method: m", "A note."):
            self.assertIn(part, svg)

    def test_svg_has_a_solid_background_so_it_reads_on_light_and_dark_pages(self):
        paths, _ = self.build(spec(), rows())
        svg = next(p for p in paths if p.suffix == ".svg").read_text(encoding="utf-8")
        self.assertIn("#fcfcfb", svg)

    def test_published_and_measured_bars_get_different_colours_and_a_legend(self):
        data = rows({1: {"label": "measured by Starfield"}})
        paths, _ = self.build(spec(), data)
        svg = next(p for p in paths if p.suffix == ".svg").read_text(encoding="utf-8")
        self.assertIn("#2a78d6", svg)
        self.assertIn("#eb6834", svg)
        self.assertIn("measured by Starfield", svg)

    def test_one_kind_of_label_uses_one_colour_only(self):
        paths, _ = self.build(spec(), rows())
        svg = next(p for p in paths if p.suffix == ".svg").read_text(encoding="utf-8")
        self.assertIn("#2a78d6", svg)
        self.assertNotIn("#eb6834", svg)

    def test_whiskers_are_drawn_when_an_error_column_is_given(self):
        data = rows({0: {"sd": "0.5"}, 1: {"sd": "1.25"}})
        plain, _ = self.build(spec(), rows())
        whisk, _ = self.build(spec(error_col="sd"), data)
        a = next(p for p in plain if p.suffix == ".svg").read_text(encoding="utf-8")
        b = next(p for p in whisk if p.suffix == ".svg").read_text(encoding="utf-8")
        self.assertNotEqual(a, b)

    def test_same_input_gives_identical_svg_and_alt_text(self):
        a, _ = self.build(spec(), rows())
        b, _ = self.build(spec(), rows())
        for x, y in zip(sorted(a), sorted(b)):
            if x.suffix != ".png":
                self.assertEqual(x.read_bytes(), y.read_bytes(), x.name)

    def test_mixed_dates_stop_the_build_before_any_file_is_written(self):
        data = rows({1: {"read_date": "2026-10-27"}})
        d = tempfile.mkdtemp()
        write_csv(d, data)
        p = Path(d) / "spec.json"
        p.write_text(json.dumps(spec()), encoding="utf-8")
        out = Path(d) / "out"
        with self.assertRaises(chart.ChartError):
            chart.build(p, out)
        self.assertFalse(out.exists() and any(out.iterdir()))


class PriceDataTest(unittest.TestCase):
    def test_public_price_file_has_only_the_allowed_columns(self):
        with PRICE_CSV.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, ALLOWED_PRICE_COLUMNS)

    def test_public_price_file_has_one_read_date(self):
        with PRICE_CSV.open(newline="", encoding="utf-8") as f:
            dates = {r["read_date"] for r in csv.DictReader(f)}
        self.assertEqual(dates, {"2026-10-20"})


class PostOneChartTest(unittest.TestCase):
    def setUp(self):
        self.spec = chart.load_spec(SPEC_1)
        self.bars = chart.load_bars(self.spec, ROOT)

    def test_the_four_bars_match_the_draft(self):
        got = [(b["name"], b["value"], b["label"]) for b in self.bars]
        self.assertEqual(got, [
            ("xAI grok-4.7", 6.0, "published by xAI"),
            ("Anthropic Claude Sonnet 5.5", 10.0, "published by Anthropic"),
            ("OpenAI gpt-6.1-sol", 10.0, "published by OpenAI"),
            ("Google gemini-4-argon", 10.0, "published by Google"),
        ])

    def test_the_title_states_the_lowest_and_highest_output_price(self):
        values = [b["value"] for b in self.bars]
        title = self.spec["title"]
        self.assertIn(f"${min(values):g}", title)
        self.assertIn(f"${max(values):g}", title)

    def test_all_four_models_list_two_dollars_input_as_the_note_says(self):
        with PRICE_CSV.open(newline="", encoding="utf-8") as f:
            data = list(csv.DictReader(f))
        for bar in self.spec["bars"]:
            row = next(r for r in data
                       if (r["provider"], r["model"]) == (bar["provider"], bar["model"]))
            self.assertEqual(float(row["input_usd_per_1m"]), 2.0, bar["name"])
        self.assertIn("$2.00 per 1M input tokens", self.spec["note"])

    def test_committed_svg_and_alt_text_match_a_rebuild(self):
        if not HAVE_MPL:
            self.skipTest("matplotlib is not installed")
        with tempfile.TemporaryDirectory() as d:
            chart.build(SPEC_1, Path(d))
            for name in ("001-output-price.svg", "001-output-price.alt.txt"):
                self.assertEqual((ROOT / "charts" / "2026-10-20" / name).read_bytes(), (Path(d) / name).read_bytes(), name)
        self.assertEqual((ROOT / "charts" / "2026-10-20" / "001-output-price.png").read_bytes()[:8],
                         b"\x89PNG\r\n\x1a\n")

    def test_google_price_file_has_the_allowed_columns_and_one_read_date(self):
        path = ROOT / "data" / "prices" / "price-sheet-2026-10-22-google.csv"
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ALLOWED_PRICE_COLUMNS)
        self.assertEqual({r["read_date"] for r in rows}, {"2026-10-22"})
        self.assertEqual(len(rows), 4)

    def test_site_spec_differs_only_in_id_height_and_links(self):
        repo, site = json.loads(SPEC_1.read_text()), json.loads(SPEC_1_SITE.read_text())
        for key in ("id", "links", "height"):
            repo.pop(key, None)
            site.pop(key, None)
        self.assertEqual(repo, site)

    def test_site_chart_footer_has_site_links_and_is_1200_by_630(self):
        if not HAVE_MPL:
            self.skipTest("matplotlib is not installed")
        with tempfile.TemporaryDirectory() as d:
            chart.build(SPEC_1_SITE, Path(d))
            svg = (Path(d) / "001-output-price-site.svg").read_text(encoding="utf-8")
            png = (Path(d) / "001-output-price-site.png").read_bytes()
            for name in ("001-output-price-site.svg", "001-output-price-site.alt.txt"):
                self.assertEqual((ROOT / "charts" / "2026-10-20" / name).read_bytes(), (Path(d) / name).read_bytes(), name)
        for part in ("starfield.net/research/methodology/", "starfield.net/research/corrections/",
                     "starfield.net/assets/research/price-sheet-2026-10-20.csv"):
            self.assertIn(part, svg)
        self.assertNotIn("docs/methodology.md", svg)
        self.assertEqual(struct.unpack(">II", png[16:24]), (1200, 630))


if __name__ == "__main__":
    unittest.main()
