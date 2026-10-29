# Run record

Every real run writes a small JSON file next to its CSV: `results/vllm-<run_id>.record.json` or `results/llamacpp-<run_id>.record.json`. It says what hardware and build produced the numbers. The code is `harness/run_record.py`. Install the one dependency with `pip install -r requirements.txt`. Nothing here has been run on a GPU yet.

The record is written before any server or benchmark starts. If a field is missing or empty, the run stops there.

## Fields

- `run_id`, `run_date_utc`: the same run id as the CSV, and the start time as `YYYY-MM-DDTHH:MM:SSZ`.
- `engine`, `engine_version`: `vllm` or `llama.cpp`, and the version or build tag you passed.
- `cuda_runtime_version`: the CUDA toolkit version of the engine build. You state it with `--cuda-runtime`, for example `12.9`.
- `model_id`, `model_revision`, `precision`: the model, its pinned commit, and `fp16` or the quant name.
- `prompt_set_id`, `prompt_set_sha256`: which prompt set vLLM used.
- `settings`: every setting of the run as the script saw it. For vLLM this includes the full server command and the bench command, so the prefix caching and thinking flags are on record. It also holds the harness git commit and whether the working tree had uncommitted changes (`harness_dirty`).
- `hardware.driver_version`: the NVIDIA driver, read from NVML.
- `hardware.cuda_driver_version`: the newest CUDA the driver supports, read from NVML.
- `hardware.gpus`: one entry per GPU with `name`, `total_vram_bytes` and `compute_capability`.

The file is sorted JSON, so the same record always gives the same bytes.

## Two different CUDA versions

`cuda_driver_version` is what the driver can support. NVML reports it. `cuda_runtime_version` is the toolkit the engine was built with. NVML cannot see it, so you state it. They often differ, and both matter. Neither is checked against the other.

## Why `_v2` for the driver CUDA version

We call `nvmlSystemGetCudaDriverVersion_v2`. The first version returns a made-up "known supported" number when `libcuda` is missing. `_v2` raises an error instead. We want the error.

## Left out on purpose

- GPU UUID: it identifies one physical card. This repo is meant to go public, and the UUID adds nothing to the result.
- Power and energy: these are measurements, not facts about the run. Some GPUs and VMs report "not supported" for them. They do not belong in a record that must always be complete.

## llama.cpp records

`llama-bench` feeds random tokens and does not read our prompt set. So `prompt_set_id` and `prompt_set_sha256` are empty strings for llama.cpp, and the record refuses any other value.

## Checked on a V100 (28 Oct 2026)

- NVML returned real values on the rented machine: name, 34359738368 bytes of memory, compute capability 7.0, a driver version and CUDA driver version 13.0.
- NVML lists every GPU on the machine and ignores `CUDA_VISIBLE_DEVICES`. The first run, on one GPU of several, recorded all of them. `read_hardware` now records only the GPUs named in `CUDA_VISIBLE_DEVICES`, and stops with an error if the variable cannot be read as GPU indices.

## Unverified until a GPU run

- That the run stops early, as intended, with a clear error on a machine with no GPU.
- That `pynvml` returns text, not bytes, in this pinned version. This was read in the source, not run.
