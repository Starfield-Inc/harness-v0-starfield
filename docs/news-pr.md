# News pull request script

The script opens a pull request on the site repository for one news page. A person-supervised session writes the page and its keyword row proposal. The script checks the page, builds the site with the page in it, and opens the pull request for a person to review. It runs no AI.

```
python3 -m harness.news_pr --page PATH --row PATH [--date YYYY-MM-DD] [--hero-variant N] [--dry-run]
```

## Inputs

| Input | What it is |
|---|---|
| `--page PATH` | The Markdown page with its front matter. The file name is the slug, for example `qwen-3-5-open-weights.md`. |
| `--row PATH` | The keyword row proposal as JSON: `slug`, `title`, `type`, `hub_link`, `primary_query`, `secondary_queries` (a list), `source_url_and_date`, `answers_question`. |
| `--date` | The day of the run, used in the run log name. The default is today's UTC date. |
| `--hero-variant N` | The cover layout, from 0 to one less than the number of variants in the site's `deploy/graphics.mjs`. Without it the script takes the variant of the newest dated news item that has a cover, plus one, as the daily job does for price pages. |
| `--dry-run` | Run the local pre-check only and print what a real run would do. It clones, writes, pushes and posts nothing. |

## Environment

The script loads the env file named by `HARNESS_DAILY_ENV`, as the daily job does.

| Variable | Use |
|---|---|
| `HARNESS_SITE_REPO_URL` | The site repository to clone and push the branch to. |
| `HARNESS_SITE_WORKDIR` | The folder for the clone and the pull request body file. |
| `HARNESS_GH_CONFIG_DIR` | The gh login used for git and gh. |
| `HARNESS_CHROME` | The Chrome binary. The script passes it to the site as `CHROME`. |
| `HARNESS_NEWS_DIR` | The news folder. It holds `news-rows/<slug>.json` and the run logs. It must be outside any git repository. The default is a `news` folder next to `HARNESS_DAILY_LOG_DIR`, or `harness-news` in the system temp folder. |
| `RADAR_SLACK_TARGET` | `main` posts to `SLACK_WEBHOOK_URL_MAIN`. Anything else, or unset, posts to `SLACK_WEBHOOK_URL_TEST`. This is the radar's switch, not `SLACK_TARGET`. |

A run takes the lock `harness-news-pr` and waits for the network, as the radar does. A dry run does neither.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A pull request exists for the item: the script opened it, or it was already open. A dry run whose pre-check passed. |
| 1 | No pull request. The output and the run log say why. |
| 2 | Bad arguments, an unreadable env file, or a news folder inside a git repository. |

## The checks

Every step is a check with the result pass, FAIL or FLAG. A FAIL makes the pull request a draft. A FLAG asks the reviewer to look and does not make a draft.

### 1. Pre-check, before any clone

| Check | FAIL when |
|---|---|
| file name | The file name is not lowercase words joined by hyphens, is longer than 60 characters, or is not the row's `slug`. |
| front matter | `title`, `description`, `type`, `date`, `updated` or `source` (`name` and an https `url`) is missing, a date is not YYYY-MM-DD, or the page or row type is not `coverage`. |
| draft and approval | `draft` is not `true`, or `approved_by` or `approved_on` is present. Only `npm run news:approve` writes the approval. |
| description length | The description is longer than 160 characters. |
| primary query in title | A word of the row's `primary_query` is in neither the title nor the first heading, when that heading is an H1. |
| hub link | `links` does not hold exactly one hub link, or that link is not the row's `hub_link`. |
| link text | A link in `links`, or a body link into `/news/`, has generic text such as here, read more, learn more, this, link or Starfield. |
| no superlatives | The title or description uses best, fastest, cheapest, largest, biggest, strongest, ultimate, world-class, industry-leading, number one, first, #1, or most advanced, powerful, popular, trusted, reliable or innovative. |
| sources | FLAG only: the body links no https source. |

These rules mirror the blocking rules of the site's SEO checker. Any FAIL here stops the run. Nothing is cloned, written or pushed, and the reasons are printed.

### 2. The clone

The script looks for an open pull request on branch `news-<slug>` first. Then it checks whether that branch is already on the site repository. Then it clones the site repository into `HARNESS_SITE_WORKDIR` and makes `news-<slug>` from `origin/main`. It copies the page to `content/news/<slug>.md` and writes `content/graphics/<slug>.json` as `{"hero": {"variant": N}, "blocks": []}`. If either file is already on main, the check `news files` fails, because a job never edits an existing item.

### 3. The site's own commands

They run in this order from the clone's root. The first one that fails is a FAIL with the end of its output, with local paths removed. The commands after it are not run and show as FAIL.

1. `npm ci`
2. `npm run content:check`
3. `npm run build`
4. `CHROME=<HARNESS_CHROME> npm run graphics -- heroes <slug>`. The check also fails when no `assets/news/<slug>/hero.png` or no `heroes.json` entry was written.
5. `npm run graphics -- check`
6. `npm run pages`. Its diff goes into the branch.
7. `UPDATE_GOLDEN=1 node --test deploy/seo.golden.test.mjs`. Its diff goes into the branch. When `CI` is set, this step is not run and is a FAIL.
8. `npm run seo:check`
9. `npm test`

### 4. The keyword row

The script reads `deploy/seo/news-keyword-map.csv` in the clone, the pinned copy of the SEO map. When a row for `/news/<slug>/` is there, the check `keyword row` passes and records its status (proposed or approved). When there is no row, `keyword row` is a FLAG: "no row in the pinned keyword map yet; the page cannot go live until the SEO session's row is merged and the pin is bumped".

### 5. The allowed files

After `git add -A`, the files that differ from `origin/main` must all be in this list:

- `content/news/<slug>.md`
- `content/graphics/<slug>.json`
- `assets/news/<slug>/` and the files in it
- `assets/news/heroes.json`
- `deploy/seo.golden.json`
- `press/index.html`, which `npm run pages` rewrites when the list of published news changes

Any other file is a FAIL "files outside the news item". The added lines are also scanned for private text with the daily job's scan. Either FAIL stops the run before the commit, so nothing is pushed. After a private text FAIL the clone is deleted.

### 6. The pull request

The script commits as the harness author with the message `News page: <title>`. It pushes `news-<slug>` without force, and opens the pull request `News: <title>` with `gh pr create`. The pull request is a draft when any check failed.

## What it never touches

The script never merges, never turns on auto-merge, never force-pushes, never deletes a branch and never deploys. It never pushes to main and never edits an existing news item. It never writes the keyword map and never opens a pull request on the SEO repository. Every git and gh call goes through the daily job's guarded runner, which refuses a merge, force or auto-merge token.

When a pull request for the branch is already open, the script reports its link and changes nothing. When the branch is on the site repository but has no open pull request, for example after a rejected and closed pull request, the script stops, deletes nothing and says so. Open the pull request from that branch by hand, or delete the branch yourself, then run again.

## How to read the pull request

The body has, in order:

1. One sentence that says what the pull request adds.
2. A sources table. Each row is one https link on the page, with the table row or sentence it backs. Open each link and check that it says what the claim says.
3. The checks table.
4. "Needs your look", the list of FLAGs.
5. One hidden line for the later Slack button listener, for example `<!-- news-pr slug=<slug> row_in_map=no status=none -->`. `status` is proposed, approved or none.

## Slack

After it opens a pull request, the script posts a text message to the radar's channel: "News PR ready for review: <title>", the pull request link, the failed checks and then the flagged ones, whether the keyword row is in the pinned map, and "Approve or reject on the PR once buttons are added; for now review the PR on GitHub". It posts no `@here`. A Slack failure is printed and does not change the exit code. A pull request that was already open is not posted again.

The Approved and Not approved buttons come later. The message is built by one function, `harness.news_pr.slack.build_message`, so it can return Block Kit then.

## The SEO row handoff

1. Every real run whose pre-check passes writes `news-rows/<slug>.json` in the news folder, a copy of `--row`. Running again writes the same file.
2. The SEO session reads it and adds the row to the SEO repository's keyword map by pull request. A person approves that pull request.
3. The site session runs `npm run sync:seo` to bump the pinned map, then regenerates the golden file.
4. Until then the news pull request shows the `keyword row` FLAG. A draft page with no row only warns in the site's SEO check. The page cannot publish until the row is in the pinned map.
