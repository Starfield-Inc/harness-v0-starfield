# vLLM run

`harness/vllm_run.py` runs the fixed prompt set against a vLLM server and writes raw CSV. It needs a GPU machine with vLLM installed, and the bench extra: `pip install "vllm[bench]==0.19.1"`. One real run, on 28 Oct 2026 with Qwen3-8B on one V100 at vLLM 0.19.1, wrote a CSV and a run record.

## What it runs

For each repeat (3 by default) it starts `vllm serve`, waits for `/health`, runs `vllm bench serve` once, and stops the server. A fresh server per repeat keeps the repeats independent. The CSV has one row per repeat.

## Settings and why

- Precision is always fp16 (`--dtype float16`). The V100 has no bf16.
- Prefix caching is off (`--no-enable-prefix-caching`), so repeated prompt text cannot speed up later requests.
- The context limit is 4096 tokens (`--max-model-len 4096`) for every model. The longest prompt plus the 256-token output cap is about 1,300 tokens, so no prompt is cut. Without it, granite-4.2-8b reserves memory for 131072 tokens and fails to start on a 32 GB card. The first Qwen3-8B run (commit f185959) used the model's default limit and is rerun with this one.
- The record lists the installed `vllm`, `torch` and `transformers` versions, because a model can load with one transformers version and fail with another.
- The model is pinned with `--revision`. The CLI refuses to run without it.
- vLLM is pinned by you. Pass `--vllm-version`. A real run stops if it differs from the installed vLLM. The same recipe is used for 0.19.1 and 0.32.0.
- Thinking is switched off for Qwen3 and Granite ids with `--default-chat-template-kwargs '{"enable_thinking": false}'`. Olmo has no thinking mode.
- The server applies the chat template. The bench uses `--skip-chat-template` so it is not applied twice.
- `--disable-shuffle` keeps the prompts in file order. `--seed 0` and `--temperature 0` make the runs greedy. Greedy runs at concurrency above 1 are still not guaranteed to give bit-identical text.
- Output is capped at 256 tokens (`--custom-output-len`).
- No request rate by default, so requests are sent as fast as the concurrency limit allows. Use `--request-rate` to change that.
- The prompt-set id and hash come from `prompts/material.json` and `prompts/prompts.jsonl.sha256`. The prompt count is the line count of `prompts/prompts.jsonl`.

A run fails if any prompt does not complete or any metric is missing from the bench output.

## Dry run

```
python3 -m harness.vllm_run --model Qwen/Qwen3-8B --revision <commit> \
  --vllm-version 0.19.1 --max-concurrency 8 --dry-run
```

This prints the server and bench commands for every repeat and runs nothing. In the printed bench commands, `RUN_ID` stands in for the real run id.

A real run is the same command without `--dry-run`. It writes `results/vllm-<run_id>.csv` and one raw JSON file per repeat.

## Checked on a V100 (28 Oct 2026, Qwen3-8B, vLLM 0.19.1)

- `--default-chat-template-kwargs` turned thinking off: a test question got a 5-token answer with no reasoning text.
- `--no-enable-prefix-caching` was accepted, and the cache query and hit counters stayed at 0 after two identical requests.
- vLLM 0.19.1 serves Qwen3-8B in fp16 on one V100. It uses the `TRITON_ATTN` backend, because FlashAttention 2 needs compute capability 8 or higher.
- All 150 requests completed in each of 3 repeats, and the three repeats agree to within 0.1 percent on output tokens per second.

Setup notes from that run:

- The system Python on the host had no `Python.h`, and Triton needs it to compile a helper. A standalone Python from `uv` has the header and worked.
- The bench client loads the tokenizer by model name, with no revision flag in our command. It looks for the `main` branch, not the pinned commit. On 28 Oct `main` and the pinned commit were the same. For a later run, check that they still are, or point the local `main` reference at the pinned commit.
- Set `CUDA_VISIBLE_DEVICES` to the GPU the run uses. The run record then lists only that GPU.

## All three models on a V100 (28 Oct 2026, vLLM 0.19.1, transformers 4.57.6, torch 2.10.0)

Qwen3-8B, Olmo-3-7B-Instruct and granite-4.2-8b each ran 3 repeats with 150 of 150 requests completed, at concurrency 8, on one V100, with a context limit of 4096. Granite answered a test question with no reasoning text when thinking was off. For all three, the prefix-cache counters stayed at 0 after two identical requests.

Two things went wrong at first, and both are now handled:

- **Olmo-3 failed to load** with `KeyError: 'rope_theta'` under transformers 5.21.0. vLLM 0.19.1 excludes only transformers 5.0.x, so 5.21 is allowed but untested. With `transformers<5` (4.57.6) Olmo-3 loads. All three models use that pairing, and the run record lists the versions.
- **Granite failed to start** because its default context of 131072 tokens needs more memory than a 32 GB card has left. The 4096 limit fixes it.

Also fixed: the CSV `cli_call` column now shows `--result-dir RESULT_DIR`, not the folder the run used.

## Not verified yet

- That `--no-enable-prefix-caching` is accepted at vLLM 0.32.0. That version is for the A100, which we do not have.
- Qwen3.5-9B, the fallback model.
- Output length varies by model. Generation stops at end-of-sequence, so a model can stop before the 256-token cap. Compare `total_output_tokens` before comparing speed between models.
- Hardware details are not in the CSV. They are in the run record file next to it (`results/vllm-<run_id>.record.json`), linked by run id.
- `total_input_tokens` and `total_tok_per_s` are too low. The bench counts tokens of the raw prompt and does not count the template tokens the server adds. The amount differs by model, so do not compare these two columns between models. The output side (`output_tok_per_s`, TTFT, TPOT, ITL) is not affected.
- The `vllm` command is found through PATH. The version check reads the Python package, so make sure both come from the same install.
- If a run fails part way, `results/` keeps the record file and the raw JSON of finished repeats but no CSV. A record without a CSV means a failed run.
- Before a run the script checks that `prompts/prompts.jsonl` matches its hash and that port 8000 is free.
