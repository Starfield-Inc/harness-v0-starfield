# Fixed prompt set

Set id: `model-watch-prompts-v1`

## What it is

The set has 150 prompts in 3 buckets of 50 each.

| Bucket | What it is | Length |
| --- | --- | --- |
| short | One standalone question or task | 9 to 37 words |
| medium | One passage plus one instruction | 130 to 161 words |
| long | Five passages plus one instruction | 631 to 684 words |

The prompts are in `prompts/prompts.jsonl`. Each line is one JSON object with `id`, `bucket`, `prompt` and `words`.

## How it was made

All text was written for this project from scratch. No outside sources were used. The source text is in `prompts/material.json`: 50 short prompts, 20 passages and 12 instructions.

A script, `harness/promptset.py`, builds the set from that file. It picks items by the sha256 hash of a fixed string, not by random numbers. So every rebuild gives the same bytes, on any machine and any Python 3.10 or newer.

## Why

- No licence problems. We wrote every word, so we can publish all of it.
- Same prompts every run. Nothing is picked by hand after the fact.
- Three lengths. Short prompts mostly test decode speed. Long prompts mostly test prefill speed. Medium sits between them.

## Rebuild and check

Rebuild the files:

    python3 -m harness.promptset

Check that the committed files match a rebuild at seed 0:

    python3 -m harness.promptset --check

The check exits with 1 and prints the problems if anything differs.

## How to cite it

- `prompt_set_id` is the set id above, `model-watch-prompts-v1`.
- `prompt_set_sha256` is the hash in `prompts/prompts.jsonl.sha256`. Use the hex value only.

Drafted with AI assistance (Claude). Review by Ian: pending.
