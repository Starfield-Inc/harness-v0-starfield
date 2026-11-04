# News pull request script

The script opens a pull request on the site repository for one news page. A person-supervised session writes the page and its keyword row proposal. The script checks the page, builds the site with the page in it, and opens the pull request for a person to review. It runs no AI.

```
python3 -m harness.news_pr --page PATH --row PATH [--key ORG/FAMILY] [--date YYYY-MM-DD] [--hero-variant N] [--dry-run]
```

## Inputs

| Input | What it is |
|---|---|
| `--page PATH` | The Markdown page with its front matter. The file name is the slug, for example `qwen-3-5-open-weights.md`. |
| `--row PATH` | The keyword row proposal as JSON: `slug`, `title`, `type`, `hub_link`, `primary_query`, `secondary_queries` (a list), `source_url_and_date`, `answers_question`. |
| `--key ORG/FAMILY` | The radar family key of the item, for example `qwen/qwen3-5`, as the radar's Slack post names it after "go for". Not approved writes it to `rejected.json`. Without it the marker says `key=none`, and Not approved closes the pull request but records nothing. |
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

Every step is a check with the result pass, FAIL or FLAG. A FAIL holds back the Approved button for the life of the pull request. A FLAG asks the reviewer to look and does not hold it back.

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

The script commits as the harness author with the message `News page: <title>`. It pushes `news-<slug>` without force, and opens the pull request `News: <title>` with `gh pr create --draft`. The pull request is always a draft. Only the Approved button, or a person on GitHub, marks it ready.

## What it never touches

The script never merges, never turns on auto-merge, never force-pushes, never deletes a branch and never deploys. It never pushes to main and never edits an existing news item. It never writes the keyword map and never opens a pull request on the SEO repository. Every git and gh call goes through the daily job's guarded runner, which refuses a merge, force or auto-merge token.

When a pull request for the branch is already open, the script reports its link and changes nothing. When the branch is on the site repository but has no open pull request, for example after a rejected and closed pull request, the script stops, deletes nothing and says so. Open the pull request from that branch by hand, or delete the branch yourself, then run again.

## How to read the pull request

The body has, in order:

1. One sentence that says what the pull request adds.
2. A sources table. Each row is one https link on the page, with the table row or sentence it backs. Open each link and check that it says what the claim says.
3. The checks table.
4. "Needs your look", the list of FLAGs.
5. One hidden last line for the Slack button listener, for example `<!-- news-pr slug=<slug> key=qwen/qwen3-5 row_in_map=no status=none checks=pass -->`. `key` is the family key or `none`. `status` is proposed, approved or none. `checks` is `pass` when every check of the run passed, else `fail`. The listener acts only on a pull request whose body ends with exactly one such line.

## Slack

After it opens a pull request, the script posts a card to the radar's channel through the incoming webhook. `RADAR_SLACK_TARGET` picks the channel, as for the radar. It posts no `@here`. A Slack failure is printed and does not change the exit code. A pull request that was already open is not posted again.

The card has, in order:

1. The pull request title, "News: <title>", its link, and one status line: whether the run's checks passed, whether the keyword row is on the branch, and the GitHub checks (running, passed or failed).
2. On the first post, the failed checks and then the flagged ones, when there are any.
3. When Approved is not offered, one line that says why: "Approved is not offered: ...".
4. The buttons, and the line "Buttons work while the listener Mac is on. If Slack shows an error, click again later."

The card is built by one function, `harness.news_pr.slack.card`. The first post and every later update use it. Every text in the card goes through the same scrub as the daily job, and links to github.com are kept.

### The buttons

| Button | What a click does |
|---|---|
| Approved | Runs the site's own approval on the pull request branch, rebuilds, runs the site checks, commits, pushes without force, and marks the pull request ready. It never merges. You merge by hand on GitHub, and the merge starts the site's Deploy. |
| Not approved | Asks first: "Close PR #n and never suggest <key> again?". Then it adds the family key to `rejected.json` in the radar folder and closes the pull request. The branch is kept. |
| Check again | Reads the pull request again and redraws the card. It changes nothing. |

The buttons come from the same rule that handles a click. A button is shown only when a click on it would be carried out now. Approved is shown when the run's checks passed, no GitHub check failed on the newest commit, and the keyword row is on the branch. Check again is shown only when Approved is held back for a reason that can change.

Right after a click, only you see "Working on Approved for PR #n, clicked 14:05 EAT. This takes a few minutes. A second click is safe." The card changes when the work is done. Times are the Mac's local time with the zone name, with the day added when it is not today.

Only the Slack member named by `SLACK_APPROVER_ID` can use the buttons. Anyone else sees "Only the news reviewer can use these buttons." and nothing changes.

### What a click does in each state

The listener reads the pull request and a fresh clone of its branch when it handles a click. It never trusts the message. The first row that matches wins.

| # | The pull request | Approved | Not approved | Check again | Buttons after |
|---|---|---|---|---|---|
| 1 | Not found, another author, a branch that is not `news-<slug>`, from another repository, or no valid marker | Refused with the reason | Refused | Refused | none |
| 2 | Merged | Nothing changes: the Deploy publishes it | Nothing changes. Revert on GitHub if needed | Nothing changes | none |
| 3 | Closed, its key not yet in `rejected.json` | Refused: reopen it on GitHub, then press Check again | Records the key (repairs a half done Not approved) | Redraws | Not approved, Check again |
| 4 | Closed, its key recorded or no key | Refused: same | Already not approved | Redraws | Check again |
| 5 | Open, page not approved, Approved held back | Refused with the reason | Records the key, then closes | Redraws | Not approved, Check again |
| 6 | Open, page not approved, nothing holds it back | Runs the approval, then marks it ready | Records the key, then closes | Redraws | Approved, Not approved |
| 7 | Open, page approved, still a draft, a GitHub check failed | Refused: the approval commit is pushed but a GitHub check failed on it | Records the key, then closes | Redraws | Not approved, Check again |
| 8 | Open, page approved, still a draft | Marks it ready (finishes an approval that stopped after the push) | Records the key, then closes | Redraws | Approved, Not approved |
| 9 | Open, page approved, ready | Already approved, ready to merge | Records the key, then closes (withdraw before merging) | Redraws | Not approved |

Approved is held back, in this order, when a check failed when the pull request was opened (close it on GitHub, fix the page and run the script again), when a GitHub check failed on the newest commit, or when the keyword row is not on the branch. For the last one: when the row is on main, press "Update branch" on the pull request page on GitHub, then press Check again. If GitHub reports a conflict, close the pull request on GitHub (not with Not approved, which would reject the key), delete the branch on GitHub, and run the script again.

| Case | What happens |
|---|---|
| Another user clicks | Only they see the refusal. No file is saved and nothing is read or changed. |
| A button the listener does not know, or a message that is not a click | Ignored, with one log line. |
| Two clicks on the same pull request | The later click replaces the earlier one if the earlier one has not started. One running approval finishes first, then the later click is handled on fresh facts. |
| A click on an old copy of the card | Handled on fresh facts. The card is replaced by the current one. |
| A click while the Mac is off or the listener is stopped | Slack shows its own error. Nothing is saved. Click again later. |
| A saved click older than 24 hours | Dropped, with a note in the channel. Click again if it is still wanted. |
| The news PR script is running | The click waits. The listener tries again every 60 seconds. |
| A step fails (a site check, the gate, the push, gh) | The work stops, nothing more is pushed, and the card names the step. The click is not tried again. Click again when it is fixed. |
| The click's reply link has expired (after 30 minutes) | The new card is posted as a new message that starts "Update on PR #n:". |

### How the listener works

The listener is a LaunchAgent, `net.starfield.harness-news-listener`, that runs `python3 -m harness.news_pr.listen`. It opens a Socket Mode connection to Slack with the app-level token. Slack sends each click over it and must get an answer (an ack) within 3 seconds. After the ack Slack never sends that click again. So the listener first saves the click to `clicks/pr-<n>.json` in the news folder, then acks, then posts the working note, then starts `python3 -m harness.news_pr.listen --drain` as a child process. The drain takes the `harness-news-pr` lock, the same lock the PR script takes, and handles the saved clicks one at a time, oldest first. A click file is removed only after its result is posted.

If the Mac, the listener or the drain stops in the middle, the click file is still there. The next drain reads the pull request again and does only the steps that are missing. The listener starts a drain when it starts and every 60 seconds while a click file waits.

Only one listener runs at a time. A second one finds its lock `harness-news-listener` held, logs that, and exits with code 1. A missing or malformed `SLACK_APP_TOKEN` or `SLACK_APPROVER_ID`, or a missing `HARNESS_SITE_REPO_URL`, `HARNESS_SITE_WORKDIR` or `HARNESS_GH_CONFIG_DIR`, is one log line and exit code 2. launchd then tries again every 60 seconds.

The listener logs one line per message from Slack: the kind, the pull request number, the button and the result. A log line never holds the token, a webhook URL, a reply link or the click's content.

### What the listener never does

It never merges, never turns on auto-merge, never force-pushes, never reopens a pull request, never updates the branch from main, never deletes a branch and never deploys. It never edits the approval lines by hand: only the site's `npm run news:approve` writes them, and a gate refuses an approval commit that changes anything in the page other than `approved_by`, `approved_on` and `draft`. It never removes a key from `rejected.json`. It has no bot token and posts only through the click's reply link or the incoming webhook. Every git and gh call goes through the daily job's guarded runner.

### Set up Slack (the owner, once)

1. Open api.slack.com/apps and pick the app that owns the daily job's webhooks.
2. Before you change anything, open Features, Incoming Webhooks. For each webhook, write down on paper its channel and the last 6 characters of its URL. Do not copy a URL into chat, a repository or a ticket.
3. Open Settings, Socket Mode, and turn it on. Slack asks for an app-level token. Name it `news-listener`, add the scope `connections:write`, and generate it. The token starts with `xapp-`. If Slack does not ask, open Settings, Basic Information, App-Level Tokens, Generate Token and Scopes.
4. Open Features, Interactivity & Shortcuts, and turn it on. With Socket Mode on, Slack asks for no Request URL. Save.
5. Find your member id: in Slack, open your profile, the three dots menu, Copy member ID. It starts with `U`.
6. Add two lines to the env file named by `HARNESS_DAILY_ENV`. The file must be outside any repository, with mode 600 (`chmod 600 <file>`):

   ```
   SLACK_APP_TOKEN=xapp-...
   SLACK_APPROVER_ID=U...
   ```

7. Check the webhooks still work: post one test message through the test webhook (`python3 -m harness.slack_notify run --day <today> --check "pass|webhook test|after Slack app changes"` with `SLACK_TARGET` unset). It must appear in the test channel.
8. Install the listener and run the `launchctl bootstrap` line it prints. Use the same Python as the daily job's and the radar's LaunchAgents (the Homebrew Python). `/usr/bin/python3` is too old to import the harness.

   ```
   scripts/launchd/install.sh --job listener --python /opt/homebrew/bin/python3 --repo /path/to/this/repo \
     --logdir /path/to/logs --envfile /path/to/daily.env
   ```

   The installer writes `~/Library/LaunchAgents/net.starfield.harness-news-listener.plist`. It never runs launchctl. launchd starts the listener at load, restarts it when it stops (at most once a minute), and has no schedule. `--hour` and `--minute` are refused for it. Logs go to `harness-news-listener.out.log` and `harness-news-listener.err.log` in the log directory. `--job listener --uninstall` prints the `launchctl bootout` command and the file to delete.
9. Within a minute the out log shows `connected`. Check that no secret reached the logs: `grep -E 'xapp-|hooks.slack.com|wss://' <logdir>/harness-news-listener.*.log` must find nothing. A certificate error in the log means this Python cannot check Slack's TLS certificate. Fix that first.

**A reinstall banner.** Socket Mode and Interactivity add no OAuth scope, and an app-level token belongs to the app, not to the install. Slack asks for a reinstall when scopes change. Uninstalling the app or revoking its tokens removes its incoming webhooks. Whether Slack shows a reinstall banner after steps 3 and 4, and whether a reinstall keeps the webhook URLs, was not checked. So:

- If no banner appears, do not reinstall. Nothing touched the webhooks.
- If a banner appears, do not click it. First do steps 7 to 9. If the test post arrives and the listener logs `connected`, leave the banner.
- Only if one of those fails, reinstall. Then open Incoming Webhooks again, compare each channel and the last 6 characters with your notes, and send one test post through the test webhook only. Do not test-post to the main channel without the owner's say.

### Recovery

To finish saved clicks by hand, for example after the listener was stopped, run this on the Mac from the repository:

```
python3 -m harness.news_pr.listen --drain
```

It runs the same code as the listener's child. It is safe while the listener runs: if a drain or the PR script holds the `harness-news-pr` lock, it exits at once with code 0. It settles only clicks the approver made, so it adds no new way to approve.

## The SEO row handoff

1. Every real run whose pre-check passes writes `news-rows/<slug>.json` in the news folder, a copy of `--row`. Running again writes the same file.
2. The SEO session reads it and adds the row to the SEO repository's keyword map by pull request. A person approves that pull request.
3. The site session runs `npm run sync:seo` to bump the pinned map, then regenerates the golden file.
4. Until then the news pull request shows the `keyword row` FLAG. A draft page with no row only warns in the site's SEO check. The page cannot publish until the row is in the pinned map.
