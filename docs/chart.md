# Chart builder

`harness/chart.py` turns a small spec file and a CSV into one bar chart. It writes three files: an SVG, a PNG and a plain-text description (alt text). The house style is fixed in the code, so every chart looks the same.

## Where charts live

Each chart sits in a folder named for the date of its data: `charts/2026-10-20/` holds chart 001 (prices read on 20 Oct 2026) and `charts/2026-10-22/` holds chart 002 (prices read on 22 Oct 2026). A new read gets a new dated folder. Old folders are never edited. The builder looks for the data file in the spec's folder and then in each folder above it, so a spec can sit at any depth.

## Build the chart for post 1

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-chart.txt
.venv/bin/python -m harness.chart charts/2026-10-20/001-output-price.json
```

This writes `charts/2026-10-20/001-output-price.svg`, `.png` and `.alt.txt` next to the spec. Use `--out-dir DIR` to write somewhere else. The script prints the paths it wrote. It needs matplotlib 3.11.2 (see `requirements-chart.txt`).

## The spec file

A spec is a JSON file. All keys below are required except `error_col`.

- `id`: file name stem for the three output files.
- `title`: the headline, drawn at the top. It should state the finding.
- `data`: path to the CSV, relative to the repo root (or to the spec file).
- `date_col`: the CSV column that holds the read or run date.
- `date_text`: the date sentence shown in the footer.
- `value_col`: the CSV column with the bar values.
- `value_format`: a Python format string for the value labels, for example `${:g}`.
- `axis_label`: the text under the x axis. It also names the unit in the alt text.
- `label_col`: the CSV column with the source label of each row.
- `error_col`: optional. A CSV column with an error size. When set, each bar gets a whisker for value plus and minus the error. Use `null` for no whiskers.
- `bars`: the bars, top to bottom. Each has `provider`, `model` (exact match with the CSV) and `name` (the text shown).
- `note`: a short footer note.
- `links`: the footer line with links to the method, the data and the corrections log.
- `height`: optional. The image height in pixels, from 500 to 1200. The default is 675 (1200 x 675). Use 630 for a 1200 x 630 social card.

Chart 001 has two specs. `charts/2026-10-20/001-output-price.json` links to repo paths. `charts/2026-10-20/001-output-price-site.json` has the same data and text, a 630 px height and starfield.net links in the footer. Its files have the id `001-output-price-site`.

## Rules the script enforces

The script checks everything before it writes any file. If a check fails it stops with a message and writes nothing.

**One date.** The whole input CSV must have one date, not only the charted rows. Only the first 10 characters of `date_col` are compared, so `2026-10-22T10:00:00Z` and `2026-10-22T11:30:00Z` count as the same date. If it finds more than one date, it lists them.

**Two allowed labels.** Every charted row needs a label in `label_col`. It must be either `published by <source>` or exactly `measured by Starfield`. Anything else, or an empty label, stops the build. Published bars are blue and measured bars are orange. A legend appears only when both kinds are on the chart. The label is always printed under the bar name, so colour is never the only signal.

Other stops: a missing spec key, a bar that is not in the CSV (or appears twice), and a value that is not a number.

## Reuse for measured runs

For a measured chart, write the run results to a CSV with the label `measured by Starfield`, point a new spec at it, and set `error_col` to the column with the spread (for example a standard deviation) to get whiskers. `date_col` can hold run timestamps. Only the date part is compared, so all runs of one day pass the one-date rule.

## What the tests cover

The SVG and the alt text are the same on every run, so the tests compare them byte for byte. The PNG is not byte-compared, because font rendering can differ between machines. The tests only check that it is a real PNG that is at least 1000 pixels wide.
