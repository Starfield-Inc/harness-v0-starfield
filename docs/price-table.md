# Price table

`harness/price_table.py` turns two dated price CSVs into one Markdown table for the daily Model Watch price post. It uses only the Python standard library.

## Run it

```
python3 -m harness.price_table \
  --today data/prices/price-sheet-2026-10-22.csv \
  --previous data/prices/price-sheet-2026-10-20.csv \
  --out price-table.md
```

The script writes the file to `--out` and prints the same text. Without `--out` it only prints. `--main-models PATH` picks a different list of main models (the default is `prices/main-models.json`).

The previous file can be from any earlier date, not only the day before. The heading names both dates. Without `--previous`, the Change column says `first read`. An example is in [`examples/price-table-2026-10-22.md`](examples/price-table-2026-10-22.md).

## Layout

1. A heading line: `Prices read on <today>. Change is against the read on <previous>.`
2. **Main models.** The models listed in `prices/main-models.json`, in the order of that file. A model is matched by provider and model name.
3. **All other models.** Every other row, grouped by provider. Providers come in the order they first appear in today's file. Models stay in file order.

The columns are Provider, Model, Input (USD per 1M tokens), Output (USD per 1M tokens), Change and Label.

If a section has no rows, it says "No models in this section."

## Scheduled price steps

A row such as `gemini-4-argon (Standard) from 2026-12-01` is a scheduled price, not a model. It does not get a table row. It becomes a note under the table that holds its model. The table shows the price in force on the read date. A step row whose model is not in the same file stops the script.

## The Change column

- `no change`: input and output are the same as in the previous file.
- `input $2 -> $3` or `output $10 -> $12`: the price that changed. If both changed, both are shown, input first.
- `new`: the provider and model are not in the previous file.
- `first read`: no previous file was given.

Models that were in the previous file and are not in today's file are listed in one line under the tables: `Not read today: ...`. Step rows are not listed there.

## How prices print

A whole number prints with no decimals: `$10`, `$2`. Any other number prints with at least two decimals: `$0.10`, `$2.50`, `$0.075`. The script never rounds. Trailing zeros beyond two decimals are dropped. The rule applies to the table and to the Change column.

## What stops the script

If a check fails, the script prints a message, exits with an error and writes nothing. The checks apply to both files unless noted.

- The file must have one date. Only the first 10 characters of `read_date` count, the same rule as `harness/chart.py`.
- The previous date must be earlier than today's date.
- The header must match the 10 columns of the price sheet exactly.
- Every label must be `published by <source>` or `measured by Starfield`.
- Input and output prices must be positive numbers.
- A provider and model pair may appear only once.
- Today's file must have every model in `prices/main-models.json`.
- The file must have at least one row.

## Main models file

`prices/main-models.json` has one key, `models`. It is a list of objects with `provider` and `model`. The order of the list is the order of the table.

## Tests

```
python3 -m unittest tests.test_price_table
```

The tests check the section order, the grouping, each kind of change, the note for a step row, each stop, and that the same input gives the same bytes. One test checks that the example file matches the script output for the two real CSVs.
