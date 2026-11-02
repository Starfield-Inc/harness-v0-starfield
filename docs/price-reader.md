# Price reader

`harness/price_reader.py` reads the public pricing pages of the providers in the price sheet and writes one new dated CSV, `data/prices/price-sheet-YYYY-MM-DD.csv`. The date is the UTC date of the read. The file has the same 10 columns as the older sheets.

## Run it

```
python3 -m harness.price_reader
```

It needs only the Python standard library. Options: `--config` (default `prices/sources.json`) and `--out-dir` (default `data/prices`).

Run it once a day. It sends one request per page, with no retry and no crawling. There are 10 page addresses for 30 models, plus 2 carried-forward Mistral rows (see below), so a sheet has 32 rows. If a sheet for today's UTC date already exists, it stops before it sends any request and leaves the file alone.

The User-Agent names the project and gives a contact address.

## What is in `prices/sources.json`

For each page: the provider, the address, the unit the page uses, a text the page must contain to confirm that unit (`unit_check`), and an optional `page_date` pattern. For each model: an `anchor` (text that marks where the model starts) and either a `row` pattern or an `input` and an `output` pattern. The patterns look only from the anchor to the next model on the same page. No price is stored in the config or in the code.

- `mode` is `text` (the visible text of the page), `html` (the raw HTML, with HTML escapes decoded) or `markdown` (the page as the provider serves it in Markdown, unchanged). The `how_read` column says `Script: page text`, `Script: raw HTML` or `Script: page markdown`. OpenAI and xAI have the prices in page data, not in a visible table, so they are read as raw HTML. Mistral is read from `https://docs.mistral.ai/inference/pricing.md`: since 2 Nov 2026 the HTML text of its pricing page shows only some rows, while the Markdown copy of the same page has every row as one table line, such as `| [Mistral Large 4](...) | ... | Input $0.7 /M Tokens<br>Cached input $0.07 /M Tokens<br>Output $2.1 /M Tokens |`. The row pattern skips the cached-input price.
- `unit` is `usd_per_1m` (no change), `usd_per_1k` (multiply by 1,000) or `usd_per_1m_x10000` (divide by 10,000). Any other value stops the run. The xAI page data holds each price as a whole number that is 10,000 times the USD per 1M tokens; the page's own text for grok-4.7 shows $2.00 and $6.00 for the data values 20000 and 60000.
- `unit_check` must match the page text. If the page says "per 1K tokens" and the config says per 1M, the run stops. It does not guess.
- `step` (Google) holds patterns for a scheduled price change. When the page shows one, the reader adds a row named `<model> from <date>`. When the page shows none, no extra row is written. If it finds only the input or only the output, or two different dates, it stops.
- `anchors` (Google) replaces `anchor` with a list of anchors, one for each layout the page has used. The reader tries them in order and uses the first one it finds. If it finds none, it stops and the error lists every anchor it tried. A model has either `anchor` or `anchors`, not both. Google's page was a rendered page until 2 Nov 2026, where the model name sits on the line above "Try it in Google AI Studio". Since then the same address serves Markdoc source, where that line follows a button tag such as `{% button href="...?model=gemini-4-argon" variant="ais" %}`, several lines below the model name. Each Google model lists the old anchor first and the new one second, so the read still works if Google goes back to the rendered page. The search ends at the next model's anchor (any of its anchors) or after `window` characters (default 800), whichever comes first. Google models use a `window` of 2500, because a Markdoc block is longer.
- `after` (OpenAI) starts the search after a marker, so the first price table (Standard) is used.
- `page_date_optional` (Google) makes the page date optional. When the page shows a date, the reader writes it as usual. When the page shows none, the reader writes `none` in `page_date_if_shown` and goes on. Without this flag, a missing page date stops the run. Google's page stopped showing its "Last updated" line on 2 Nov 2026.

Labels are `published by <Provider>`. Where the provider is a host (DeepInfra, Together AI, Groq, Fireworks), the label names that host.

## Carried-forward rows

On 2 Nov 2026 Mistral took Mistral Large 3 and Mistral Medium 3.5 off its pricing page. The owner chose to keep both on the sheet, so a reader of the daily table still sees them, but with the price we last read and a label that says so. They are listed under `carried` in the Mistral block of `prices/sources.json`. Each entry has `model`, `input`, `output`, `carried_from` (the date of the last read), `source_url` (the pricing page) and `reason`.

The reader writes each entry as a row with:

- the price from the config, not from the page;
- `how_read` = `Carried forward from the <carried_from> read, no longer on the provider's page`;
- `label` = `published by Mistral, not on the page on <read date>`;
- `page_date_if_shown` = `none`.

Why the label: the price is still one Mistral published, so the label starts with `published by`, but it is not today's price on the page. The label and `how_read` say this on every row, so a reader never takes it for a fresh read. The daily checks flag carried rows every day (`carried rows`), and the price table shows `carried forward from <date>` in the Change column with one note under the tables.

A carried row is valid only while the model is absent from the page. If the page names the model again, the run fails and asks the owner to remove the `carried` entry and read the model from the page. This way an old price never stands in for a real one.

When a provider adds or drops a model, the `same rows` check fails unless the change is listed in `prices/row-changes.json` for that sheet's date. The 2026-11-02 entry lists the new Mistral Large 4 row.

## What makes the run fail

Any of these stops the run with a message that names the provider and the model, and writes no file:

- a page cannot be fetched (network error or HTTP error);
- a model is not found on its page;
- a price is not found or is not a number;
- a price is zero or below;
- the input price is above the output price;
- the unit check fails or the config names an unknown unit;
- a page date pattern is set, `page_date_optional` is not set, and the page has no date;
- a carried entry has no `carried_from` date (or no model, price, source or reason);
- a carried model is on the page again;
- the run crosses midnight UTC, so the rows would not share one date.

The reader never guesses and never copies a value from an older file. The only old values on a sheet are the carried rows, which come from the config and say so in their label. If one provider's page changes layout, the whole run fails and the config for that provider has to be fixed by a person.

## Tests

`python3 -m unittest tests.test_price_reader` runs without network. It uses small hand-written snippets in `tests/fixtures/price_reader/`, and one saved provider page: `mistral_pricing_2026-11-02.md`, the Mistral Markdown page as served on 2 Nov 2026. The Mistral tests run the real Mistral block of `prices/sources.json` against it. A test also checks that `prices/sources.json` covers the 31 models of `price-sheet-2026-10-20.csv`, with the same provider, model and page address, apart from the 2 Nov 2026 Mistral changes.

## Limits

- A script read is only as good as the pattern. A pattern could match the wrong table if a page is redesigned in a way that keeps the same words. Compare each new sheet with the last one and look at any price that changed.
- Pages that need JavaScript to show prices are not supported. A headless browser (Playwright) is not used. All 10 pages return their prices in the HTML that a plain request gets.
- Only the Standard price for short prompts is read. Cached, batch, priority and long-prompt prices are not.
- Check each provider's terms of use for scripted reads before you run this on a schedule.
