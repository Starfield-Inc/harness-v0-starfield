# Live check of the Google Gemini price page

Checked on 22 Oct 2026. This note belongs to `price-sheet-2026-10-20.csv`. We do not edit that file, so the check is recorded here.

**What was checked.** The Google rows in the sheet came from an archived (Wayback) copy. On 22 Oct 2026 we opened the live page, https://ai.google.dev/gemini-api/docs/pricing, once, in a headless Chromium browser driven by Playwright. The page loaded without a sign-in redirect (HTTP 200).

**What the live page showed for gemini-4-argon, Standard, paid tier, per 1M tokens in USD.**

| Item | Live page | Sheet |
|---|---|---|
| Input | $2.00 through 30 Nov 2026, $4.00 from 1 Dec 2026 | 2 |
| Output | $10.00 through 30 Nov 2026, $20.00 from 1 Dec 2026 | 10 |
| Page date | "Last updated 2026-10-19 UTC" | 2026-10-19 |

The live page and the archived copy agree on the two prices used in post 1 (input $2, output $10), on the page date, and on the rise on 1 Dec 2026.

**What this does not cover.** We checked only gemini-4-argon (Standard). We did not re-check the other Google rows, or the xAI, Anthropic and OpenAI rows. The chart still says "read by hand on 20 Oct 2026", which is when the numbers were read. A person should still look at the page in a normal browser before the post goes out.
