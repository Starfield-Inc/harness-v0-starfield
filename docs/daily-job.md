# The daily job

The daily job reads prices, measures the models on one GPU, checks the results, and opens a pull request on the site repository. A person reviews the pull request. The job never merges and never deploys.

## What the job does each day

The job runs these steps in order.

1. It reads the price pages and writes the day's price sheet. The reader can only read today's UTC date. `--date` defaults to that date. For any other date the job reads no prices and records the FAIL check `price read`, unless the sheet for that date already exists. The other steps still run.
2. It runs every model in `config/models.json` on one GPU with the pinned vLLM, using the fixed prompt set.
3. It runs the checks listed below on the price sheet and on the run files.
4. It commits the new data in a separate `git worktree` of this repository, on the temporary branch `daily-data-YYYY-MM-DD`, which starts from `origin/main`. It never commits in your checkout, never switches your branch and never changes your index.
5. It builds the site branch (price page, cover image, prices index, harness manifest) and runs the site's own commands and checks on it.
6. If no check has failed, it pushes the data commit with `git push origin daily-data-YYYY-MM-DD:main`. The push is fast-forward only. If any check has failed, it pushes nothing.
7. It scans the lines it added to the site files for private text (see "Private text in the site files" below). If it finds any, it stops here: it pushes no site branch and opens no pull request.
8. It pushes the site branch and opens a pull request on the site repository with the new pages and a table of the check results.
9. It removes the temporary worktree and the temporary branch, whether the run passed, failed or stopped with an error.

### Your checkout after a push

The job brings your local `main` up to date with `git merge --ff-only origin/main`, but only when all of these are true.

- This run pushed the data commit, or `origin/main` already held the day's data and no check of this run failed. When an earlier push or another machine put the data on `origin/main` and a check of this run failed, the job leaves your checkout alone.
- Your checkout is on `main`.
- Your local `main` has no commits that are not on `origin/main`.
- Each of the day's data files in your checkout is untracked and the same as the pushed copy. The job removes these copies first, because they would block the fast-forward. If the fast-forward fails, it puts them back.

Staged changes to other files stay staged. In any other case, the job leaves your checkout alone, and the pull request and the printed report say why, for example "The harness checkout on this machine is not on main, so local main was not updated."

Commits on your local `main` are never pushed. The data commit starts from `origin/main`, not from your branch.

If a run stops part way, for example because the process was killed, the temporary worktree or branch can stay behind. The next run of the same day removes them before it starts. Nothing in them is public unless `origin/main` already holds it.

If any check fails, the pull request opens as a draft. A draft cannot be merged by accident. When the data was not pushed, the first line of the pull request says "Data was not pushed to the public repository because checks failed."

### When the job opens no pull request

The job needs the day's price sheet and an earlier sheet to build the price page. If either sheet is missing, the job opens no pull request and pushes nothing. The same happens when the site files hold private text, when the GitHub CLI cannot list or open pull requests, for example because its login has expired, and when the publish step stops with any error. The job then does these things.

1. It writes the report, with host and account details removed, to `harness-daily-YYYY-MM-DD.log` in `HARNESS_DAILY_LOG_DIR`, or in the system temp directory when that key is not set.
2. It shows a macOS notification that names the log file. If the notification fails, the job goes on.
3. It exits with code 1.

If the data was already pushed when the error happened, for example when the push of the site branch failed, the report says "The data is already public" and names the harness commit.

The job also writes the local report and shows the notification in these other cases.

- A step (prices, GPU or publish) stops with an error. The job records it as the FAIL check `price step`, `gpu step` or `publish step`.
- The job does not start because another run holds the lock.
- Any other error stops the job. The job prints one scrubbed line to standard error and exits with code 1. It never prints a Python traceback, because standard error is the launchd error log and a traceback holds local paths. Even an import error, for example under the wrong Python, prints only `harness.daily stopped:` and the error name.
- The env file cannot be read, because it is missing or is not UTF-8. The job prints one scrubbed line and exits with code 2.

If the folder in `HARNESS_DAILY_LOCK_DIR` does not exist, the job makes it. If the job cannot make it, or the file system cannot lock (`flock` fails with an error), the job does not start and does not use another folder, because two runs could then lock two different files. It exits with code 1, and the local report and the notification say why.

If the GPU step did not run (`--skip-gpu`, or the step stopped with an error), the job records the FAIL check `gpu step` and still opens a draft pull request with the price page.

The GPU step copies the run files back into a staging folder next to the launch record. It empties that folder before every copy, and rsync skips hidden files (`--exclude=.*`), so a temp file of an interrupted rsync or a `.DS_Store` cannot fail a later copy back. A link, a device, a nested folder or any other file that is not a run file still fails the copy back. The stop step writes to the launch log on the GPU machine only when that log already exists. Otherwise it prints its message and leaves no log, so a launch that failed at the ssh connection still counts as not launched and the same day is tried again.

### Running the job again on the same day

A second run on the same day is safe. Before it changes anything, the job looks for the day's pull request on the site repository with `gh pr list --head daily-YYYY-MM-DD`. A fork can open a pull request from a branch with the same name, so the job asks gh for `isCrossRepository` and `headRefName`, and uses only a pull request that comes from the site repository itself and from that branch. It ignores every other one, and the pull request text and the report name each one it ignored. It never changes a pull request that is not its own.

If the job's own pull request exists, the job returns it and pushes nothing. It reads the harness commit that the branch's manifest pins and says whether the data was pushed. A pull request that someone closed without a merge does not count, so closing a bad run lets the next run open a new one. A merged one counts as done.

- If `origin/main` holds that commit, the result says "Harness data pushed" and names the commit.
- If it does not, the result says "Harness data not pushed" and the pull request stays a draft.
- If the job cannot read the manifest, or cannot fetch `origin`, the result says that it is not known whether the data was pushed, and the pull request stays a draft.

In the last two cases, if someone marked the pull request ready for review, the job turns it back into a draft with `gh pr ready --undo`.

If the branch `daily-YYYY-MM-DD` exists on the site repository but has no pull request, an earlier run stopped part way. The job opens a draft pull request from that branch as it is, with the same pushed or not pushed sentence. The FAIL check `site checks` says that the site checks were not run again.

The data commit is also safe to repeat. The job fetches `origin` and compares each of the day's files with `origin/main`. If `origin/main` already holds all of them, the job makes no commit and no push, and uses the commit that added them.

### The cover image

The site's tests need a cover for each research page. After the price page and the manifest check, the job writes the graphics spec `content/graphics/research/prices-YYYY-MM-DD.json` with `{"hero": {"variant": N}, "blocks": []}`. N is the variant of the newest earlier price page's spec plus one. After the last variant it starts again at 0, and with no earlier spec it is 0, so two price pages in a row never share a cover layout. The job reads the number of variants from the `VARIANTS` list in the site's `deploy/graphics.mjs`. It then runs `npm run build`, because the cover embeds the site's fonts, and `npm run graphics -- heroes research/prices-YYYY-MM-DD`, which renders `assets/research-covers/prices-YYYY-MM-DD/hero.png` (1280 by 720) with headless Chrome and adds the page to `assets/news/heroes.json`. Research pages have no card image. The job passes the Chrome path from `HARNESS_CHROME` to the site as `CHROME`. The check `cover image` passes and names the files. If Chrome is not there, a command fails, or no cover was written, the check fails with the reason, the job goes on, and the pull request opens as a draft. If the spec, the cover and its `heroes.json` entry are already on the site's main branch, the job makes nothing again. If only the spec is there, the job keeps its variant and renders the cover.

### Private text in the site files

Before it pushes the site branch, the job scans every line it added to the site files, except the golden file and the PNG images, which are not text. The FAIL check `no host details in site files` lists the kinds of text it found, never the text itself. The scan looks for these things.

- The privacy patterns of the run files: a home path, `/data/`, an IPv4 address, an at sign, or the word ssh.
- Each word in `HARNESS_PRIVATE_TERMS` and each value of the host and path keys, as a whole token, the same way the scrub matches them.
- Host names that the scrub removes, outside web links. Web links stay out of this rule, because the price sheets link to each provider's pricing page.
- Local folders such as `/Users/...` and paths that start with `~/`. Other absolute paths stay out of this rule, because the site pages use site paths such as `/research/prices/`.
- Secret tokens and user names or passwords in links.

If the check fails, the job does not push the site branch, even as a draft, because a pushed branch can be read by everyone who can read the site repository. It opens no pull request, deletes its clone of the site repository, and writes the local report and shows the notification as described in "When the job opens no pull request". The harness data is not pushed either, because a check failed.

### Pull request text

The whole pull request text goes through the scrub in `harness/scrub.py` before it leaves the machine. This includes the summary, the notes from the GPU step, and every check detail. The title is fixed text and the date, so it does not go through the scrub. The job then scans the text with the privacy patterns of the run files. Each line that still matches is replaced with `<removed>`, and the FAIL check `PR body privacy` drafts the pull request.

The scrub removes these things.

- Each value of the host and path keys in the env file, and each word in `HARNESS_PRIVATE_TERMS`, in any case. The scrub removes a word only as a whole token, that is, when no letter or digit comes right before or after it. With the word `2222`, the link `commit/9f2222ab` stays. When the word is part of a host name or a path (letters, digits, dots, dashes, underscores and slashes around it), the scrub removes the whole host name or path. With the word `gpu`, all of `gpu-01.lan` goes.
- The user name and password in a link, that is, everything between `://` and `@`, even in a link to github.com.
- Any text of the form `something@something`, unless it is an email address on one of the safe hosts below, or a name with a version after the at sign: `torch@2.4` and `actions/checkout@v4` stay (a version starts with a digit or `v` and a digit, and the name before it has no dot). `root@node7.lan` and `root@10.1.2.3` go. `344872025+esteban657@users.noreply.github.com` stays.
- Text that looks like a secret, wherever it is: tokens that start with `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`, `sk-`, `xox` or `AKIA`, and private key blocks (`-----BEGIN ... PRIVATE KEY-----`).
- IPv4 addresses.
- Absolute paths with two or more parts, such as `/srv/bench/out`, and paths that start with `~/`. A relative path such as `harness/daily_gpu.py` stays.
- Host names with two or more dots, unless they are on github.com, raw.githubusercontent.com, starfield.net, huggingface.co, pypi.org or docs.vllm.ai. Version numbers such as `0.19.1`, file names such as `content.golden.json`, and Python names such as `requests.exceptions.ConnectionError` stay.
- Web links to any other host. Links to the hosts above, and site links such as `[corrections log](/research/corrections/)`, stay as they are.

At the start of each run, the job runs `ssh -G` on the `HARNESS_GPU_SSH` alias. It adds the host name, the alias, the base name of each key file (such as `bench_key`) and the user to `HARNESS_PRIVATE_TERMS` for that run. It adds the user only when the name has 3 or more characters and is not `root`, `ubuntu`, `admin`, `user`, `pi` or `ec2-user`, because those are common words. It never adds the port, because a port is not secret and a port number matches parts of commit hashes. The job adds these words so the scrub and the GPU step's privacy check catch the real host and account even when the text never names the alias. If `ssh -G` fails, the run goes on without them.

The job does not add a word, and the scrub and the site scan do not treat it as private, when it is a whole word in any tracked file of the harness repository (`git grep -iwqF`). That repository is public, and a word such as `gpu` or `v100` is in every day's real data (the `gpu` column of the speed CSV, `Tesla V100-SXM3-32GB`), so the site scan would find it every day and no pull request would open. The job goes on, prints this line, and writes the local report and the notification: "These terms were left out because they already appear in the public repository: ..., choose a unique ssh alias."

Everything the job prints also goes through the scrub. This includes check details, error text, the lock path and the report location, so the launchd log files hold no host or account details.

### Time limits and the lock

Each command the publish step runs stops after 900 seconds. `npm ci` and `npm test` get 1800 seconds. The check that the harness commit is public stops after 30 seconds.

The lock is an `flock` on the file `harness-daily-<repo folder name>.lock` in `HARNESS_DAILY_LOCK_DIR`. Only one process at a time can hold it. The system releases it when the run ends, also when the run crashes or is killed, so a lock is never left behind and no run ever takes over another run's lock. If another run holds the lock, the job does not start, and the report says which process holds it.

While a run holds the lock, the file holds the process ID of that run and the start time of the process, as `ps -o lstart=` prints it. This text only says who holds the lock. It does not decide anything. At the end, the run empties the file. The job never deletes the file, because a run could otherwise lock a file that another run had just deleted, and both runs would start.

## Review and merge

A person reads the pull request and the check table. The person decides whether to merge. The job has no merge step and no deploy step. Merging and deploying stay with people.

## Checks

Each check returns pass, FAIL, or FLAG. A check that cannot read its input returns FAIL.

### FAIL checks

A FAIL opens the pull request as a draft. These checks guard the method and the privacy of the files.

Price sheet.

- `columns`. The sheet has the ten columns in the spec, in order.
- `read date`. The file name matches `price-sheet-YYYY-MM-DD.csv`, and every row carries that date.
- `row count`. The sheet has as many rows as the previous sheet.
- `same rows`. The sheet has the same provider and model rows as the previous sheet.

A row added or removed that is listed in `prices/row-changes.json` for the sheet's date turns `row count` and `same rows` into a FLAG that quotes the note. Any other row change still fails.
- `prices present`. Every row has an input price and an output price.
- `labels`. Every label starts with `published by `, and every source URL is https.

Run files, one set of checks for each model.

- `completed`. The run has the expected number of repeats, and every repeat completed every prompt.
- `prompt hash`. The prompt set hash matches the committed hash.
- `revision`. The model revision is the one pinned in `config/models.json`.
- `versions`. The vLLM and transformers versions match the pinned versions.
- `clean commit`. The run record names a harness commit, and the working tree was clean.
- `one GPU`. The run record lists one GPU, and its name matches the expected card.
- `privacy`. The run files hold no private text, such as a home path, an IP address, an at sign, or the word ssh.

Site branch.

- `cover image`. The graphics spec and the cover of the new price page were written (see "The cover image" above). The detail names the files, or says why the cover was not made.

### FLAG checks

A FLAG does not draft the pull request. It asks a person to look before merging.

- `price changes`. A provider changed an input or output price since the previous sheet. The detail lists each change. Carried-forward rows are not compared.
- `carried rows`. The sheet has rows a provider no longer lists, kept with an earlier price. The detail names each row and the date of its price.
- `page dates`. A provider had a page date on the previous sheet and has `none` today. The detail names the provider and the last date seen. Carried-forward rows are not compared.
- `speed band`. Output tokens per second moved more than 2 percent from the mean of the earlier days.
- `token band`. Output tokens per run moved more than 5 percent from the mean of the earlier days.

On the first day for a model there is nothing to compare, and both band checks pass.

## Environment keys

The job reads its settings from an env file outside the repository. Nothing in the repository holds a host name, an account name, or a secret. The `HARNESS_DAILY_ENV` key holds the path of the env file.

The push command is not a setting. The job always pushes with `git push origin daily-data-YYYY-MM-DD:main`, from its temporary worktree. The branch your checkout is on does not matter. If the push fails, for example because `origin/main` moved and the push is not a fast-forward, the FAIL check `harness push` drafts the pull request.

| Key | What it holds |
|---|---|
| `HARNESS_GPU_SSH` | The ssh target of the GPU machine. |
| `HARNESS_GPU_WORKDIR` | The directory on the GPU machine where runs happen. |
| `HARNESS_GPU_ENV_PATH` | The path on the GPU machine of the Python environment with vLLM. |
| `HARNESS_GPU_HF_HOME` | The model cache directory on the GPU machine. |
| `HARNESS_GPU_INDEX` | The index of the one GPU the runs use. |
| `HARNESS_SITE_REPO_URL` | The clone URL of the site repository. |
| `HARNESS_SITE_WORKDIR` | The local directory where the job keeps its clone of the site repository and its temporary harness worktree. |
| `HARNESS_GH_CONFIG_DIR` | The GitHub CLI config directory whose login opens the pull request. The job also uses that login for every git command that clones, fetches, pushes or lists a remote. It passes the login in environment variables (`GH_CONFIG_DIR` and git's `GIT_CONFIG_*` credential helper), so the path is never in a command line, a log or a report. With no working login, git fails at once and does not ask. |
| `HARNESS_DAILY_ENV` | The path of the env file itself. |
| `HARNESS_DAILY_LOCK_DIR` | The directory for the lock that stops two runs at once. |
| `HARNESS_DAILY_LOG_DIR` | The directory for the local report when the job opens no pull request, when a step stops with an error, or when the lock is held. The default is the system temp directory. |
| `HARNESS_CHROME` | The path of the Chrome program that renders the cover image. The default is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. If the file is not there, the `cover image` check fails. |
| `HARNESS_DRY_RUN` | `1`, `true`, `yes` or `on`, in any case, turns on dry run. The GPU step lists its commands and runs none. The publish step makes no commit, no push, no clone and no pull request, and prints what it would do. Any other value, or no value, is a normal run. |
| `HARNESS_PRIVATE_TERMS` | Comma-separated words that must never leave this machine, such as the real host name and the account name. The scrub removes them from the pull request, the local report and the printed output, and the privacy check fails if a result file holds one. The job adds the values from `ssh -G` for each run, except words that already appear in the public repository. |
| `SLACK_WEBHOOK_URL_TEST` | The Slack webhook of the test channel. The job posts here unless `SLACK_TARGET` is `main`. |
| `SLACK_WEBHOOK_URL_MAIN` | The Slack webhook of the main channel. Used only when `SLACK_TARGET` is `main`. |
| `SLACK_TARGET` | `main` posts to the main channel. Any other value, or no value, posts to the test channel. |

## Slack messages

The job posts two kinds of message. Both are text only, in Slack formatting. The code is in `harness/slack_notify.py`.

Message A is the daily run summary. The job posts it after the publish step. It has the title "Model Watch daily run, D Mon YYYY" and no `@here`. The Findings section has one line per model (output tokens per second, the value of the earlier day, first token time) and one line about prices: "No price changed since D Mon YYYY." or the changes, at most 8, then "and N more changes". The Checks section says "All checks passed", or lists the failed and flagged checks, at most 6 of each. The Review section links the pull requests and, for a draft, says "Draft: checks failed, do not merge until fixed". If the GPU step produced no summary, the message says there is no speed data.

Message B tells people that pages are live. It starts with `@here`, has a Findings section (the same lines as message A, left out when no CSV is given) and a Live pages section with one line per page. The site's deploy workflow posts it after the pages are live, with the same builder, through `python3 -m harness.slack_notify_site --pages pages.json --assets <site checkout>/assets/research`. That command reads one webhook from `SLACK_WEBHOOK_URL`, so the workflow picks the main or test webhook. `python3 -m harness.slack_notify live ...` builds the same message from explicit paths.

Rules for both messages.

- Every message goes through the scrub before it is posted. Messages longer than 3000 characters are cut and end with `...`.
- With no webhook the job prints "no webhook, skipping" and goes on.
- A failed post prints only the error name. The webhook URL is never printed or logged.
- Slack can never change the exit code of the job.
- In a dry run (`HARNESS_DRY_RUN`) the job prints message A, scrubbed, and posts nothing.

## Run it by hand

Run a dry run from the repository root.

```
python3 -m harness.daily --dry-run
```

`--dry-run` runs no step at all. It prints the steps that would run. A dry run is the safe first test of a new env file. To run the steps without a push or a pull request, set `HARNESS_DRY_RUN=1` instead. Run without either to do the full job once.

## Install the schedule

The schedule is a macOS LaunchAgent. It runs `python3 -m harness.daily` at the set time each day and each time you log in, in your login session, so the keychain holds the ssh key the job needs. The files are in `scripts/launchd/`.

1. Write the env file outside the repository, with the keys above.
2. Run the installer with your own paths. The hour and minute are optional and default to 6 and 0.

```
scripts/launchd/install.sh --python /path/to/python3 --repo /path/to/this/repo \
  --logdir /path/to/logs --envfile /path/to/daily.env --hour 6 --minute 30
```

3. The installer writes `~/Library/LaunchAgents/net.starfield.harness-daily.plist` and prints a `launchctl bootstrap` command. Paths can contain `&`, `<` and `>`. The installer does not run that command. Run it yourself to load the job.

The login run covers a day when the Mac was off at the set time. A rerun on the same day is safe. It reuses the day's prices and GPU runs, does not touch the host, and posts no Slack message when it finds its pull request and has nothing new. A login before the providers update their pages reads that day's earlier prices.\n\nStandard output and standard error go to two files in the log directory. To remove the job, run `scripts/launchd/install.sh --uninstall`. It prints the `launchctl bootout` command and the file to delete.

## Add a model

Edit `config/models.json`. Put a model in `core` to run it every day, or in `rotating` for the rotating set. Each entry has an `id`, a pinned `revision`, and a `thinking_off` flag.

Check these once before you add the entry.

- The licence allows running the model and publishing the speed numbers.
- The weights fit on one 32 GB card in fp16, with room for the cache.
- The model loads on the pinned vLLM.
- Thinking is off where the model has a thinking mode, and `thinking_off` is `true`.
- The entry pins a revision, as a full commit hash.

A model that needs more than one GPU is out of scope for this job. The `one GPU` check fails on it.
