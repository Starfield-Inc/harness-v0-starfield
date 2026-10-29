# Price reader

`harness/price_reader.py` reads the public pricing pages of the providers in the price sheet and writes one new dated CSV, `data/prices/price-sheet-YYYY-MM-DD.csv`. The date is the UTC date of the read. The file has the same 10 columns as the older sheets.

## Run it

```
python3 -m harness.price_reader
```

It needs only the Python standard library. Options: `--config` (default `prices/sources.json`) and `--out-dir` (default `data/prices`).

Run it once a day. It sends one request per page, with no retry and no crawling. There are 10 page addresses for 31 models. If a sheet for today's UTC date already exists, it stops before it sends any request and leaves the file alone.

The User-Agent names the project and gives a contact address.

## What is in `prices/sources.json`

For each page: the provider, the address, the unit the page uses, a text the page must contain to confirm that unit (`unit_check`), and an optional `page_date` pattern. For each model: an `anchor` (text that marks where the model starts) and either a `row` pattern or an `input` and an `output` pattern. The patterns look only from the anchor to the next model on the same page. No price is stored in the config or in the code.

- `mode` is `text` (the visible text of the page) or `html` (the raw HTML, with HTML escapes decoded). The `how_read` column says `Script: page text` or `Script: raw HTML`. OpenAI and xAI have the prices in page data, not in a visible table, so they are read as raw HTML.
- `unit` is `usd_per_1m` (no change), `usd_per_1k` (multiply by 1,000) or `usd_per_1m_x10000` (divide by 10,000). Any other value stops the run. The xAI page data holds each price as a whole number that is 10,000 times the USD per 1M tokens; the page's own text for grok-4.7 shows $2.00 and $6.00 for the data values 20000 and 60000.
- `unit_check` must match the page text. If the page says "per 1K tokens" and the config says per 1M, the run stops. It does not guess.
- `step` (Google) holds patterns for a scheduled price change. When the page shows one, the reader adds a row named `<model> from <date>`. When the page shows none, no extra row is written. If it finds only the input or only the output, or two different dates, it stops.
- `after` (OpenAI) starts the search after a marker, so the first price table (Standard) is used.

Labels are `published by <Provider>`. Where the provider is a host (DeepInfra, Together AI, Groq, Fireworks), the label names that host.

## What makes the run fail

Any of these stops the run with a message that names the provider and the model, and writes no file:

- a page cannot be fetched (network error or HTTP error);
- a model is not found on its page;
- a price is not found or is not a number;
- a price is zero or below;
- the input price is above the output price;
- the unit check fails or the config names an unknown unit;
- a page date pattern is set but the page has no date;
- the run crosses midnight UTC, so the rows would not share one date.

The reader never guesses and never copies a value from an older file. If one provider's page changes layout, the whole run fails and the config for that provider has to be fixed by a person.

## Tests

`python3 -m unittest tests.test_price_reader` runs without network. It uses small hand-written snippets in `tests/fixtures/price_reader/`, not saved provider pages. A test also checks that `prices/sources.json` covers the 31 models of `price-sheet-2026-10-20.csv`, with the same provider, model and page address.

## Limits

- A script read is only as good as the pattern. A pattern could match the wrong table if a page is redesigned in a way that keeps the same words. Compare each new sheet with the last one and look at any price that changed.
- Pages that need JavaScript to show prices are not supported. A headless browser (Playwright) is not used. All 10 pages return their prices in the HTML that a plain request gets.
- Only the Standard price for short prompts is read. Cached, batch, priority and long-prompt prices are not.
- Check each provider's terms of use for scripted reads before you run this on a schedule.
