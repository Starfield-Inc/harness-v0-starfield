# How Model Watch measures things

This page explains how Model Watch gets its numbers, what it leaves out, and which parts exist today. Each post will link here.

## What is done and what is planned

We have run speed tests on a GPU so far: three models with vLLM on one V100, on 28 Oct 2026, as a check of the method. No numbers from them are published yet. This page marks every part as **Done** or **Planned**.

| Part | Status |
|---|---|
| Price sheet read on 20 Oct 2026 (post 1) | Done |
| Fixed prompt set and its hash | Done |
| Chart builder with one-date and label checks | Done |
| vLLM run script and the run record | Done on one V100 with three models. |
| llama.cpp run script | Code written and tested on a laptop. No GPU run yet. |
| Speed runs on an A100 | Planned |
| Quality checks | Planned |
| Energy and cost per million tokens | Planned |

Settings marked "unverified" below have not yet been confirmed on a real GPU. If a first run shows that a setting does not work as written, we update this page and say so in the post. A mistake in a published number goes in the [corrections log](../CORRECTIONS.md).

## The two labels

Every number in a post carries one of two labels.

- **published by [source]** means the source printed the number and we copied it. These numbers are not ours. We name the source and the date we read it.
- **measured by Starfield** means we ran the test ourselves with the method on this page.

The chart builder refuses any bar that has neither label.

## What Model Watch does not claim

- It is not a ranking of models. A post says which models it compares and why.
- It says nothing about Starfield's own stack. We will publish numbers for it only after it serves real traffic.
- A price does not say how good a model is. A speed number does not say how good its answers are.
- A measured number holds for the hardware, engine version, model revision, precision and prompts named in the post. We do not claim it holds elsewhere.
- Before we publish, a second person checks every technical claim. This page is waiting for that check.

## Prices

**Status.** Done for post 1.

The first sheets (20 and 22 Oct 2026) were copied from each provider's own pricing page by hand, or read from the page's raw text or page data, and for Google on 20 Oct from an archived copy. From 28 Oct 2026 a script, `harness/price_reader.py`, reads each provider's own pricing page once and writes the sheet. It makes one request per page, and it stops without writing anything if a price is missing or cannot be read. The `how_read` column says which way each row was read. We do not use third-party price trackers.

For each price, the price sheet records the provider, the model, the input and output price in US dollars per million tokens, the page address, the date we read it, the date the page shows (when it shows one), and how we read it. The sheet for post 1 is [`data/prices/price-sheet-2026-10-20.csv`](../data/prices/price-sheet-2026-10-20.csv). It has 31 rows, and every row was read on 20 Oct 2026.

How the sheet is made: each read gets a new dated file, and we never edit an old file. We compare each new sheet with the last one, and a daily post shows the change since the earlier read and names both dates. The script and its limits are described in [the price reader note](price-reader.md), and the post layout in [the price table note](price-table.md).

Prices from different dates never share a chart. The chart builder (`harness/chart.py`) stops with an error if its input has more than one date. A table in a post may show the change since an earlier read, and it names both dates.

Limits you should know about:

- Many pages show no date. For those, the date we read the page is the date that counts.
- Some models have a higher price for long prompts. Post 1 uses the short-prompt prices.
- A provider can announce a price change. When a page shows one, the post says so. The archived copy of the Google page for gemini-4-argon shows a rise on 1 Dec 2026.
- We read the Google prices from an archived copy of the page, not the live page. The `how_read` column says "Wayback copy" for those rows. On 22 Oct 2026 we opened the live page once for gemini-4-argon, and it showed the same input and output prices and the same rise on 1 Dec 2026. The note is in [`live-check-google-2026-10-22.md`](../data/prices/live-check-google-2026-10-22.md). We did not re-check the other Google rows. The live page also shows two later price steps (gemini-4-argon from 1 Dec 2026 and gemini-3.8-flash from 1 Jan 2027). They are in a separate dated file, [`price-sheet-2026-10-22-google.csv`](../data/prices/price-sheet-2026-10-22-google.csv), so prices from different dates stay apart.
- Hosts that resell a model can charge more than the maker. When a post compares an open model, we plan to name one host.
- We plan to keep a saved copy of each page we read. The saved copies are not in this repository yet.

## Models

**Status.** Done for vLLM on one V100. The llama.cpp load check is planned.

The first three models are Qwen3-8B, Olmo-3-7B-Instruct and granite-4.2-8b. We chose them for these reasons.

- Their weights are open and need no access request. Hugging Face lists each of them under the Apache-2.0 licence (read 20 Oct 2026).
- Their size is 7.3 to 8.8 billion parameters (Hugging Face API, 20 Oct 2026). At 2 bytes each in fp16, the weights come to about 14.6 to 17.6 GB. That is our estimate, and it leaves out the memory the KV cache needs. It fits a 32 GB card.
- All three are dense models, not mixture-of-experts models. All three load in vLLM. We have not yet confirmed that each one loads in llama.cpp.

All three models load and run in fp16 on a V100 with vLLM (checked 28 Oct 2026). We have not yet checked that each one converts to GGUF and loads in llama.cpp.

Each run pins the model to an exact revision (a commit hash on Hugging Face). Both run scripts refuse to start without a revision. They do not check that the value is a real commit hash, so we copy it from Hugging Face.

## Hardware

**Status.** Done for one V100. An A100 is planned.

The speed runs so far used one NVIDIA Tesla V100-SXM3-32GB with 32 GB of memory. We plan to add one A100. Every run saves the GPU name, its memory, the driver version and the CUDA version, and each post states them.

A V100 cannot run bf16. So every comparison across GPUs uses fp16.

## Engines and versions

**Status.** vLLM v0.19.1 is in use on the V100. The other versions are planned. We pin these versions and change them only with an entry in the corrections log.

| Tool | Version | Used for |
|---|---|---|
| vLLM | v0.19.1 on the V100, v0.32.0 on the A100 | Serving speed with many requests at once |
| llama.cpp (`llama-bench`) | tag b11753 | Single-stream speed |
| lm-evaluation-harness | v0.4.13 | Quality checks |
| nvidia-ml-py | 13.615.71 | Reading GPU details |

The V100 and the A100 use different vLLM versions, because newer vLLM releases dropped V100 support. A post that shows both must say so.

## Precision

**Status.** Planned.

- fp16 is the base precision for both engines.
- 4-bit (GGUF `Q4_K_M`) is the second precision. We run it with llama.cpp.
- We may add a bf16 run on the A100 as an extra. It is always labelled.

For llama.cpp we convert each model to a GGUF file with one recipe: convert to fp16, then quantize to `Q4_K_M`. We do not use files made by others. The check that the models convert and load has not been done yet.

## The fixed prompt set

**Status.** Done.

Every vLLM run will use the same prompts. We do not choose prompts after seeing results.

- The set has 150 prompts: 50 short (9 to 37 words), 50 medium (130 to 161 words) and 50 long (631 to 684 words).
- We wrote every prompt for this project. No outside text is in the set. The text was drafted with AI help and is waiting for a second person to review it.
- A script builds the set the same way each time and saves its SHA-256 hash. The set id is `model-watch-prompts-v1`. The hash is `44ff438eaa2742fd6bbc70b09b41940dd9f66cf43022230b43fc8770ee83db8a`.
- The vLLM run script saves the hash with each run. Before it starts, it rebuilds the set, checks it against the prompt file and the hash, and refuses to run if they differ.

`llama-bench` does not read our prompts. It feeds its own random tokens: 512 tokens in and 128 tokens out. So llama.cpp rows have no prompt-set hash, and they record the 512 and 128 instead.

More detail is in [the prompt set note](prompts.md).

## How we measure speed

**Status.** vLLM script checked on one V100 with one model. llama.cpp script written and tested on a laptop only.

### vLLM

We run `vllm bench serve` against a vLLM server. For each run:

- The model runs in fp16.
- We start a fresh server for each of 3 repeats, so one repeat does not reuse the server's memory or caches from the one before.
- Prefix caching is off, so repeated text in the prompts cannot make later requests faster.
- Thinking mode is off for Qwen3 and Granite. Olmo-3 has no thinking mode.
- The server applies the chat template once.
- Prompts go in file order. Output is greedy (temperature 0) with seed 0.
- Output is capped at 256 tokens. A model can stop earlier, so the number of output tokens differs between models. We show it next to any speed number.
- The run fails if any request fails.

Each repeat gives one row. We plan to report the mean and the standard deviation of the 3 repeats. The metrics are output tokens per second, requests per second, time to first token, time per output token and the gap between tokens.

On a V100 we checked that vLLM v0.19.1 serves Qwen3-8B, Olmo-3-7B-Instruct and granite-4.2-8b, that thinking is off for Qwen3 and Granite, and that prefix caching is off. Each run uses a context limit of 4096 tokens, and the record lists the vLLM, torch and transformers versions. These models need transformers older than version 5 with vLLM v0.19.1. We have not yet checked the prefix-caching flag at v0.32.0.

Known limit. The vLLM tool counts input tokens from the raw prompt and not from the prompt with the chat template added. The input-token totals and the "total tokens per second" column are too low by an amount that differs by model. We do not compare those two columns between models. The output-side numbers are not affected.

### llama.cpp

We run `llama-bench` with 512 prompt tokens, 128 generated tokens and 3 repeats. It reports mean and standard deviation for prompt processing and for generation, in tokens per second. We set flash attention on or off explicitly, so the record shows what ran.

`llama-bench` measures one stream. It has no time to first token and no concurrent requests.

**We never put `llama-bench` and `vllm bench serve` results on the same chart.** They measure different things. They also use separate CSV files and different columns.

We have not yet verified on a GPU that flash attention works on the V100 or that each model loads.

## The run record

**Status.** Done on one V100. Each real run saves a record next to its CSV. It holds the GPU name, memory and compute capability, the driver version, the newest CUDA version the driver supports, the CUDA version of the engine build, the engine and its version, the model and its revision, the precision, the date, the prompt-set hash (vLLM only), and every setting. For vLLM the settings include the full server and benchmark commands. The record also holds the commit of this repository and whether it had uncommitted changes.

On a rented V100 machine the record held real values: GPU name, memory, compute capability, driver and CUDA versions. The machine has several GPUs, so a run sets `CUDA_VISIBLE_DEVICES` and the record lists only the GPU used.

If a field is missing, the run stops before it uses any GPU time. We leave the GPU's serial identifier out of the record on purpose.

The raw output of each tool is saved too, so any number in a CSV can be traced to the tool's own output.

## Quality, energy and cost

**Status.** Planned. None of this is built yet.

- **Quality.** We plan to use lm-evaluation-harness v0.4.13 on `hellaswag`, `arc_challenge`, `gsm8k` and `mmlu`, with a small sample limit. We will fix the number of examples shown and the sample size, and write both on the post. A blind rating by people may follow.
- **Energy.** We plan to read the GPU's energy counter over windows of several minutes, after warm-up, with 60 seconds of idle power logged first. We do not know yet whether a rented machine exposes the counter. NVIDIA gives no accuracy figure for it.
- **Cost per million output tokens.** We plan to divide the GPU price per hour by the number of output tokens the GPU makes in one hour (measured output tokens per second times 3,600 times the share of time the GPU is busy), then multiply by 1,000,000. We will show several busy-time shares. The GPU price will be labelled "published by" its provider.

## Charts

**Status.** Done for the chart builder and chart 001. No post is published yet.

Each chart shows one metric and one date. Its title states the finding. The footer gives the date, a note, and links to this page, the data and the corrections log. Each bar carries its label as text, so colour is never the only signal. The chart builder writes a text description of every chart. Each post will also show a table of the same numbers. More detail is in [the chart note](chart.md).

## Where the data lives

Raw CSV files, charts and scripts are in this repository. Fixes to a post or a number go in [the corrections log](../CORRECTIONS.md).
