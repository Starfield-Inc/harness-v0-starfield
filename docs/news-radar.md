# News radar

The radar finds new open-weight models from a fixed list of organizations and writes a ranked list of up to three candidates a day. A person picks one to write up. The radar does not post to Slack, open pull requests or write pages.

## What it reads

| Source | Used for | Has history for the backtest |
|---|---|---|
| Hugging Face models API, per org, newest first | the releases | yes (`createdAt`) |
| Hugging Face trending (`sort=trendingScore`) | signal | no, so the backtest skips it |
| OpenRouter model list | signal, and the watch list | yes (`created`), but only models served today |
| Official feeds (per org, plus the Hugging Face blog) | lab post signal | only the posts the feed still holds |
| DeepSeek change log, Z.ai release notes | lab post signal | yes, the entries are dated |
| Press feeds | two or more outlets signal | only the posts the feed still holds |

Each request uses the User-Agent `starfield-radar` and a 20 second timeout. A source that fails is listed under "Sources that failed" and the run goes on.

## The rule

A Hugging Face repo is a candidate only when all of these hold:

1. Its author is an org in `config/radar.json`.
2. The repo is public and lists weight files (a tag such as `safetensors` or `gguf`).
3. It is not a derivative. A `base_model:` tag of type `quantized`, `adapter` or `merge` makes it one. So does `finetune`, except for a fine-tune of the org's own base model of the same family (an instruct model of its base). A name marker (NVFP4, FP8, FP4, GGUF, ONNX, AWQ, GPTQ, MLX, INT4, INT8, BF16) also makes it a quantized copy.
4. It has at least one signal: on OpenRouter, on Hugging Face trending, a lab post, or two or more press outlets. A lab post or press item counts from 7 days before the repo was created up to the day of the run.
5. A minor dated update needs a lab post. A name ending in `-MMDD` or `-YYMM` (such as `-0905` or `-2507`) is a minor update. So is a release whose lab headline calls it an update, and that headline does not count as a lab post.
6. Its kind is text, vision or any-to-any. An image, audio, embedding, safety or other model that passes the rest and has a lab post is listed under "Other releases".

Variants of one model family become one candidate. The other variants are listed as "same family" and quantized copies as "quantized copy". Candidates are ranked by the number of signals, then newest first. The first three are the candidates for the day. The rest are listed as over the cap.

Name matching between a repo and an OpenRouter id or a headline is described in the `harness/radar/names.py` docstring, with its limits.

OpenRouter models from an official org that have no Hugging Face repo go to "Watch (API only)". They are never candidates.

## Files

The radar folder is `HARNESS_RADAR_DIR`. When it is not set, it is a `radar` folder next to `HARNESS_DAILY_LOG_DIR`, or `harness-radar` in the system temp folder.

| File | What it holds |
|---|---|
| `radar-YYYY-MM-DD.md` | the day's list, the same text the run prints |
| `seen.json` | family key to the first day it was suggested |
| `rejected.json` | family keys never to suggest again (read only, a list or an object) |

A family key is the author and the name words without sizes, markers, dates and words such as instruct, for example `qwen/qwen-3-5`. A key suggested on an earlier day is not suggested again. Running twice on one day gives the same list.

## Config keys

`config/radar.json` holds public data only.

| Key | Meaning |
|---|---|
| `orgs[]` | `name`, `hf_author`, `openrouter_prefixes`, `feeds` (`url`, `kind`: `rss` or `atom`), `pages` (`url`, `parser`: `deepseek_changelog`, `zai_release_notes` or `plain_links`), `github_atoms`, `verified` |
| `official_feeds` | feeds that count as a lab post for every org |
| `press_feeds` | feeds for the two or more outlets signal |
| `settings.cap` | candidates per day (3) |
| `settings.candidate_days` | how many days a new repo can still become a candidate (7) |
| `settings.signal_days_before` | how many days before a repo's creation a post still counts (7) |
| `settings.watch_days` | how many days an API-only model stays on the watch list (2) |
| `settings.hf_limit`, `settings.backtest_hf_limit` | repos read per org, live (30) and in the backtest (100) |

An org with `verified: false` has not been checked against the live APIs. Its candidates are marked "org not verified yet".

## Env vars

| Variable | Meaning |
|---|---|
| `HARNESS_DAILY_ENV` | an env file read the same way as the daily job |
| `HARNESS_RADAR_DIR` | the radar folder |
| `HARNESS_DAILY_LOG_DIR` | used to place the radar folder when `HARNESS_RADAR_DIR` is not set |
| `HARNESS_SEO_MAP` | optional keyword map CSV (`news_url`, `query`), to flag "maybe covered" |
| `HARNESS_SITE_NEWS_DIR` | optional site news folder (`<slug>.md`), to flag "maybe covered" |
| `HARNESS_PRIVATE_TERMS` | words the scrub removes from the output, as in the daily job |

"Maybe covered" only flags a candidate. It never removes one.

## How to run

```
python3 -m harness.radar                    # today, writes the files
python3 -m harness.radar --dry-run          # prints only
python3 -m harness.radar --date 2026-11-04
python3 -m harness.radar --backtest 30      # replays the last 30 days, prints only
```

Exit code 0 when at least one source answered, 1 when every source failed or on an error, 2 for bad arguments, an unreadable env file or a bad config.

## How to read the backtest

The backtest reads every source once, then replays each day of the last N days with only the signals dated on or before that day. It remembers what it suggested in memory, so a candidate counts on the first day it passes and not again. It writes no file.

Each day prints a count and the candidates with their signals and the reason they passed. The summary gives the days with 0 candidates, the mean per day, the most in one day, and the rejected repos counted by their first failed check.

The backtest undercounts, because trending has no history, feeds hold only their recent posts, and OpenRouter lists only the models it serves today. A note says when an org's 100 newest repos do not reach back to the start of the window. A mean near 1 a day with few days over 3 means the rule is not noisy. Many days at the cap means the rule is too loose.
