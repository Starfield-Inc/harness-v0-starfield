# Model Watch method specification

Updated 29 Oct 2026. This page states the method in detail. [The methodology page](methodology.md) is the plain-English version, and [the findings note](findings-2026-10-28.md) reports the first results. Prices and versions were read on 20 Oct 2026 unless a section says otherwise. Anything marked **Planned** has not been run.

## 1. Models

All three models are ungated and listed under Apache-2.0 on Hugging Face. Parameter counts come from the Hugging Face API (read 20 Oct 2026). The fp16 sizes are Starfield estimates (parameters x 2 bytes) and leave out the KV cache.

| Model | Revision | Parameters | fp16 weights | vLLM on a V100 |
|---|---|---|---|---|
| [Qwen/Qwen3-8B](https://huggingface.co/api/models/Qwen/Qwen3-8B) | `b968826d9c46dd6066d109eabc6255188de91218` | 8.19B | 16.4 GB | Ran on 28 Oct 2026 |
| [allenai/Olmo-3-7B-Instruct](https://huggingface.co/api/models/allenai/Olmo-3-7B-Instruct) | `6e5971d9eba42665f5bd5a0fcf047f299ce1dccc` | 7.30B | 14.6 GB | Ran on 28 Oct 2026 |
| [ibm-granite/granite-4.2-8b](https://huggingface.co/api/models/ibm-granite/granite-4.2-8b) | `a7042144125ce5ae64f89610e8289647fc000b9a` | 8.79B | 17.6 GB | Ran on 28 Oct 2026 |

All three are dense models and fit the 32 GB card. Qwen3-8B and granite-4.2-8b do not fit a 16 GB card in fp16. Llama 3.1 8B and Gemma 3 4B are gated, so they are left out. Loading each model in llama.cpp (GGUF conversion) is **Planned**.

## 2. Engines, precisions and versions

We pin these versions and change them only with an entry in [the corrections log](../CORRECTIONS.md). Versions were read on 20 Oct 2026.

| Item | Pin | State | Source |
|---|---|---|---|
| vLLM on a V100 | v0.19.1, CUDA 12.8 build of torch 2.10.0, `--dtype float16` | In use. Serves all three models. Needs transformers older than version 5 (Olmo-3 fails under 5.x with `KeyError: 'rope_theta'`). | [v0.19.1 CMakeLists](https://github.com/vllm-project/vllm/blob/v0.19.1/CMakeLists.txt), [v0.20.0 CMakeLists](https://github.com/vllm-project/vllm/blob/v0.20.0/CMakeLists.txt) |
| vLLM on an A100 | v0.32.0, published 18 Oct 2026 | **Planned.** Not run. | [PyPI](https://pypi.org/pypi/vllm/json) |
| llama.cpp | tag b11753, CUDA 12.8 on a V100 (12.9.x builds fail to allocate weights, open issue) | Runner written and tested. **Planned** on a GPU. | [releases](https://github.com/ggml-org/llama.cpp/releases) |
| lm-evaluation-harness | v0.4.13, 31 Aug 2026 | **Planned** | [PyPI](https://pypi.org/pypi/lm-eval/json) |
| nvidia-ml-py | 13.615.71, 25 Sep 2026 | In use for the run record | [PyPI](https://pypi.org/pypi/nvidia-ml-py/json) |

A vLLM number from a V100 (v0.19.1) and one from an A100 (v0.32.0) use different versions. Any post that shows both says so.

On a V100, vLLM uses the `TRITON_ATTN` backend, because FlashAttention 2 needs compute capability 8 or higher. The "FA2 not supported" log line is expected.

### Precision

- fp16 is the base precision on both engines and on both GPUs. A V100 cannot do bf16, so any comparison across GPUs uses fp16.
- 4-bit GGUF `Q4_K_M` is the second precision, run with llama.cpp. **Planned.**
- A bf16 run on the A100 may be added as a labelled extra.
- We do not use vLLM with GPTQ or AWQ. The vLLM quantization matrix marks AWQ as unsupported on Volta, and it conflicts with the install page, which says compute capability 7.5 or higher ([quantization docs](https://docs.vllm.ai/en/latest/features/quantization/), [install docs](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)).

## 3. Metrics and run rules

| Metric | Tool | State | Label |
|---|---|---|---|
| Price per 1M input and output tokens, with read date | Provider pages, by hand until 22 Oct 2026 and by script from 28 Oct | Done | published by [provider] |
| Output tokens per second, requests per second, time to first token (TTFT), time per output token (TPOT), gap between tokens (ITL) | `vllm bench serve` ([docs](https://docs.vllm.ai/en/latest/benchmarking/cli/)) | Done on one V100 | measured by Starfield |
| Prompt and generation tokens per second, mean and standard deviation | `llama-bench` with 512 prompt and 128 generated tokens ([README](https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md)) | **Planned** on a GPU | measured by Starfield |
| Tokens per joule: output tokens / (energy delta in mJ / 1000) | `nvmlDeviceGetTotalEnergyConsumption`, Volta or newer ([NVML docs](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html)) | **Planned** | measured by Starfield |
| Quality | lm-evaluation-harness on `hellaswag`, `arc_challenge`, `gsm8k` and `mmlu` with `--limit`, and blind ratings | **Planned** | measured by Starfield |
| Self-hosted USD per 1M output tokens = GPU price per hour / (measured output tokens per second x 3600 x utilisation) x 1,000,000, at several utilisation levels | Speed runs and a GPU price | **Planned** | measured by Starfield (speed), published by [provider] (GPU price) |

### Rules for every vLLM speed run

- The prompt set is fixed and its file hash is recorded. See [the prompt set note](prompts.md).
- 150 prompts, 8 at a time (`--max-concurrency 8`), in file order, greedy (`--temperature 0`), `--seed 0`, output capped at 256 tokens.
- 3 repeats. Each repeat starts a fresh server. Prefix caching is off (`--no-enable-prefix-caching`), because it is on by default and repeats could otherwise look faster.
- Thinking is off for Qwen3 and Granite (`--default-chat-template-kwargs '{"enable_thinking": false}'`). Olmo-3 has no thinking mode. The server applies the chat template, and the bench runs with `--skip-chat-template`.
- Every model runs with `--max-model-len 4096`. The longest prompt plus 256 output tokens is about 1,300 tokens, so no prompt is cut.
- A run fails if any request fails.
- One GPU per run, chosen with `CUDA_VISIBLE_DEVICES`. The run record lists only that GPU.
- Compare `total_output_tokens` before comparing speeds. Models stop at different lengths, and greedy output is not exact when requests run at once.
- We never put `llama-bench` (one stream, no TTFT) and `vllm bench serve` (concurrent requests) in one chart.

Energy windows will last several minutes after warm-up, with 60 seconds of idle power logged first. Whether a rented machine exposes the energy counter is unknown until tested, and NVIDIA gives no accuracy figure for it.

## 4. Sources

| Source type | Rule |
|---|---|
| Prices | Read from each provider's own pricing page, dated, with the page address. Standard short-prompt prices only. We do not use third-party trackers or the OpenRouter API as a price source. |
| Speed and optimisation | Our own runs only. Always "measured by Starfield". |
| Quality | lm-evaluation-harness, with the version, few-shot count and `--limit` written on the post. |
| Public benchmarks | Link and credit, never copy. "published by [source], read on [date]". |

Prices differ between hosts and makers. Example: DeepInfra lists claude-sonnet-5 at $3.00 in / $15.00 out, while Anthropic's page lists Sonnet 5 at $2 / $10 (both read 20 Oct 2026; [DeepInfra](https://deepinfra.com/pricing), [Anthropic](https://claude.com/pricing)). For open-versus-paid comparisons, we name one host per open model. Only Google's page shows its own date (2026-10-19) and a scheduled rise: Gemini 4 Argon goes from $2.00 / $10.00 to $4.00 / $20.00 on 1 Dec 2026 ([Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)). Each post states which price date it uses.

## 5. Charts

- One horizontal bar chart, one metric, one source date. Bars show the mean, with whiskers for the standard deviation on measured data.
- The title is the finding with its number. The footer holds the label, the date, a note, and links to the method, the data and the corrections log. A chart of measured data will also name the hardware, engine and version, model revision, precision and run date (**Planned**: no measured chart exists yet).
- A table of the same rows sits below, with the label for each number. Every chart has alt text.
- Prices from different dates never share a chart. A change since an earlier read goes in a table column that names both dates.
- Each chart lives in a folder named for the date of its data, for example `charts/2026-10-20/`. See [the chart note](chart.md).

## 6. Posts

A post title reads `Model Watch #N: [finding with its number]`. A post is about 150 words, with one chart, one short table and links to the method and the raw data. Every number carries its label.

## 7. Corrections log

The log is [`CORRECTIONS.md`](../CORRECTIONS.md), linked from every post. Newest first. Each entry has: ID (C-001), date found, date fixed, post number and URL, what was wrong (old text and number), what is right (new text and number), cause, who found it, who checked it, and any CSV or chart files changed. Fixes change the post in place with a visible note at the top, never silently.

## 8. Data files

**Price sheet** (`data/prices/price-sheet-<date>.csv`), 10 columns, one read date per file:
`read_date, provider, model, input_usd_per_1m, output_usd_per_1m, unit, source_url, page_date_if_shown, how_read, label`

**vLLM run** (`results/vllm-<run id>.csv`), one row per repeat, 29 columns:
`run_id, run_date_utc, run_type, engine, engine_version, model_hf_id, model_revision, precision, thinking_off, prompt_set_id, prompt_set_sha256, num_prompts, seed, concurrency, request_rate, output_len_cap, repeat_index, n_repeats, completed, total_input_tokens, total_output_tokens, ttft_ms_mean, ttft_ms_p99, tpot_ms_mean, itl_ms_mean, req_per_s, output_tok_per_s, total_tok_per_s, cli_call`

**llama.cpp run** (a separate file and schema), 28 columns:
`run_id, run_date_utc, run_type, engine, engine_version, engine_commit, build_number, build_flags, backends, gpu_name, model_hf_id, model_revision, model_sha256, precision, quant_format, tool_model_type, prompt_set_id, prompt_set_sha256, n_prompt_tokens, n_gen_tokens, n_repeats, n_gpu_layers, flash_attn, pp_tok_per_s, pp_stddev_tok_per_s, tg_tok_per_s, tg_stddev_tok_per_s, cli_call`

**Run record** (`results/<run id>.record.json`): the GPU name, memory and compute capability, the driver version, the newest CUDA version the driver supports, the CUDA version of the engine build, the engine and its version, the model and its revision, the precision, the date, the prompt-set hash, the installed vllm, torch and transformers versions, every setting including the full server and benchmark commands, and the harness commit. See [the run record note](run-record.md).

**Speed summary** (`data/speed/speed-<date>.csv`): one row per model, built by `harness/speed_summary.py` from the run CSVs and records, with the mean and range of the repeats.

The raw JSON from `vllm bench serve --save-result` and from `llama-bench -o json` is kept next to each run as the evidence for its rows.
