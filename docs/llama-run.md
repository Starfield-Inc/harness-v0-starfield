# llama.cpp run

`harness/llama_run.py` runs `llama-bench` on one GGUF file and writes raw CSV. It needs a GPU machine with llama.cpp built. Nothing here has been run on a GPU yet.

## What it runs

One call to `llama-bench`:

```
llama-bench -m <gguf> -p 512 -n 128 -r 3 -ngl 99 -fa <0|1> -o json
```

llama-bench measures two things. Prompt processing (pp) is how fast it reads 512 tokens. Token generation (tg) is how fast it writes 128 tokens. Both are in tokens per second. The CSV has one row per run, with the mean and standard deviation of each.

## Settings and why

- The model is a GGUF file, either fp16 (`F16`) or `Q4_K_M`. The `precision` column says `fp16` for F16 and `q4_k_m` for Q4_K_M.
- Flash attention is always set to 0 or 1. The default "auto" is logged as -1 and does not say what ran. The script refuses anything else, and refuses a result where the tool reports a different value.
- The 3 repeats happen inside llama-bench (`-r 3`). There is no server, so there is nothing to restart. A result with a different number of samples is rejected.
- All layers go on the GPU (`-ngl 99`).
- llama-bench feeds random synthetic tokens. It does not read our prompt set. So `prompt_set_id` and `prompt_set_sha256` are empty, and the real sizes (512 and 128) are recorded in `n_prompt_tokens` and `n_gen_tokens`.
- There is no time to first token and no concurrency. These columns do not exist in this CSV.
- The model is pinned with `--revision`, and the CLI refuses to run without it. The sha256 of the GGUF file is computed before the run.
- The pp and tg rows must come from the same llama.cpp build, or the run is rejected.
- The raw JSON from llama-bench is saved to `results/<run_id>.json` before the numbers are checked, so a failed check still leaves the evidence. If llama-bench itself fails, or prints something that is not JSON, the run stops and shows its error text.
- `--quant-format` must be `F16` or `Q4_K_M`. The CSV also keeps llama-bench's own description of the model in `tool_model_type`, so a wrong label can be spotted by eye. It is not checked automatically, because the exact text is unverified.

Never chart or merge this CSV with the vLLM CSV. The columns differ on purpose, and the file prefix is `llamacpp-` instead of `vllm-`. The two tools measure different things.

## Dry run

```
python3 -m harness.llama_run --model-path models/qwen3-8b-q4_k_m.gguf \
  --model-id Qwen/Qwen3-8B --revision <commit> --quant-format Q4_K_M \
  --llama-cpp-tag b11753 --build-flags "GGML_CUDA=ON CMAKE_CUDA_ARCHITECTURES=70" \
  --flash-attn 1 --dry-run
```

This prints the `llama-bench` command and runs nothing.

## Making the GGUF files

Use one recipe for all three models:

1. Convert to fp16: `python3 convert_hf_to_gguf.py <model dir> --outtype f16 --outfile <model>-f16.gguf`
2. Quantize: `llama-quantize <model>-f16.gguf <model>-q4_k_m.gguf Q4_K_M`

Build llama.cpp from a full git clone, not a shallow one. A shallow clone gives a wrong build number. Use `-DGGML_CUDA=ON` with `-DCMAKE_CUDA_ARCHITECTURES=70` for the V100 or `80` for the A100. Pass the same flags as `--build-flags` so they land in the CSV.

## Not verified until a GPU run

- That all three models convert to GGUF and load.
- That flash attention works on the V100.
- Whether casting bf16 weights to f16 overflows for any model.
- The exact `gpu_info` strings the tool reports.
