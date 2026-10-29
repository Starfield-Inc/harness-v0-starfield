# Model Watch harness

This repository holds the code, prompts, data and method behind **Model Watch**, Starfield's short research posts about AI model prices, speed and efficiency.

Every number in a post can be traced here: the dated price sheets, the raw output of each speed run, the scripts that built the tables and charts, and the method we followed. Each number carries one of two labels, **published by [source]** or **measured by Starfield**.

## Results so far

### Prices

We read each provider's own pricing page and save one dated file per read. The latest read is [`data/prices/price-sheet-2026-10-28.csv`](data/prices/price-sheet-2026-10-28.csv), with 31 rows. Earlier reads are in [`data/prices/`](data/prices/). We never edit an old file, and prices from different dates never share a chart.

### Speed on one V100

We ran three open models with vLLM 0.19.1 on one NVIDIA Tesla V100-SXM3-32GB, in fp16, on 28 Oct 2026. Each run sent the same 150 prompts, 8 at a time, three times, with a fresh server for each repeat. All requests completed. These numbers are **measured by Starfield**.

| Model | Output tokens per second | Time to first token (ms) | Output tokens per run |
|---|---|---|---|
| Qwen3-8B | 239.8 | 173.6 | 21,586 |
| granite-4.2-8b | 229.1 | 195.2 | 27,622 |
| Olmo-3-7B-Instruct | 200.9 | 210.3 | 22,533 |

This is not a ranking. The models stop at different lengths, so compare the token counts before comparing speeds. It is three models, one GPU and one engine, and speed says nothing about answer quality. Read [the findings note](docs/findings-2026-10-28.md) for the settings, the ranges and the limits. The summary file is [`data/speed/speed-2026-10-28.csv`](data/speed/speed-2026-10-28.csv), and the raw files are in [`results/`](results/).

## How we keep it honest

- Every number is labelled "measured by Starfield" or "published by [source]".
- We run the same prompts every time. No cherry-picking.
- Every run saves a raw CSV and a run record with the hardware, engine version, model revision, precision and date. The tool's own output is saved next to them.
- Mistakes go in a public corrections log: [`CORRECTIONS.md`](CORRECTIONS.md).

## What is in this repo

- [`docs/methodology.md`](docs/methodology.md): how Model Watch measures things, in plain English. It marks what is done and what is planned.
- [`docs/spec.md`](docs/spec.md): the method in detail: models, engines and versions, metrics, run rules, sources, charts and CSV columns.
- [`docs/findings-2026-10-28.md`](docs/findings-2026-10-28.md): what the first three runs showed, and what they do not show.
- [`data/prices/`](data/prices/): the dated price sheets. [`data/speed/`](data/speed/): one summary row per model.
- [`results/`](results/): raw run files, each run with its CSV and run record.
- [`charts/`](charts/): one folder per read date, for example `charts/2026-10-20/`. See [`docs/chart.md`](docs/chart.md).
- [`prompts/`](prompts/): the fixed prompt set (150 prompts) and its SHA-256 hash. See [`docs/prompts.md`](docs/prompts.md).
- [`harness/`](harness/): the code.
  - `price_reader.py` and `prices/sources.json` read the pricing pages. See [`docs/price-reader.md`](docs/price-reader.md).
  - `price_table.py` builds the daily price table. See [`docs/price-table.md`](docs/price-table.md).
  - `vllm_run.py` runs `vllm bench serve` and writes a CSV. See [`docs/vllm-run.md`](docs/vllm-run.md).
  - `llama_run.py` runs `llama-bench` and writes a separate CSV. See [`docs/llama-run.md`](docs/llama-run.md).
  - `run_record.py` records the hardware, engine, model and settings. See [`docs/run-record.md`](docs/run-record.md).
  - `speed_summary.py` builds the one-row-per-model summary.
  - `chart.py` builds the charts.
- [`tests/`](tests/): unit tests that run without a GPU.

## Run it yourself

Run the tests. The chart tests need matplotlib (`pip install -r requirements-chart.txt`) and are skipped without it.

```
python3 -m unittest discover -s tests
```

Check that the prompt set matches its hash:

```
python3 -m harness.promptset --check
```

Print the exact vLLM commands for one model without running anything. The revision is the pinned Hugging Face commit for that model:

```
python3 -m harness.vllm_run --model Qwen/Qwen3-8B --revision b968826d9c46dd6066d109eabc6255188de91218 --vllm-version 0.19.1 --max-concurrency 8 --dry-run
```

Build the price table from two dated sheets:

```
python3 -m harness.price_table --today data/prices/price-sheet-2026-10-28.csv --previous data/prices/price-sheet-2026-10-20.csv --main-models prices/main-models.json
```

A real speed run needs a GPU machine with vLLM installed. See [`docs/vllm-run.md`](docs/vllm-run.md). A V100 needs vLLM 0.19.1, because newer releases dropped V100 support.

## What is not done yet

- vLLM 0.32.0 on an A100.
- llama.cpp runs. The runner is written and tested, and it has not run on a GPU.
- Quality checks, energy use and cost per million tokens.

## Corrections and contact

If you find a mistake, email hello@starfield.net. We list every correction in [`CORRECTIONS.md`](CORRECTIONS.md).

## Licence

MIT. See [`LICENSE`](LICENSE).
