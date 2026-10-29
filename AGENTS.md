# Benchmark harness v0

**Goal:** A public harness that runs fixed prompts on a rented GPU with vLLM and llama.cpp and writes raw CSV.

## Rules
- No cherry-picking. Same prompts every time.
- V100: fp16 only, 32 GB. Recent vLLM dropped V100 kernels: pin older vLLM or use llama.cpp.
- Run the tests before a commit: `python3 -m unittest discover -s tests`.
- Keep host details, account details and private data out of the repo.
