# The daily job

The daily job reads prices, measures the models on one GPU, checks the results, and opens a pull request on the site repository. A person reviews the pull request. The job never merges and never deploys.

## What the job does each day

The job runs these steps in order.

1. It reads the price pages and writes the day's price sheet. The reader can only read today's UTC date. `--date` defaults to that date. For any other date the job reads no prices and records the FAIL check `price read`, unless the sheet for that date already exists. The other steps still run.
2. It runs every model in `config/models.json` on one GPU with the pinned vLLM, using the fixed prompt set.
3. It runs the checks listed below on the price sheet and on the run files.
4. It commits the new data to this repository. The commit stays local for now.
5. It builds the site branch (price page, prices index, harness manifest) and runs the site's own commands and checks on it.
6. If no check has failed, it pushes the data commit to `main` of this repository. If any check has failed, it pushes nothing and undoes its own local commit. The files stay in the working tree.
7. It opens a pull request on the site repository with the new pages and a table of the check results.

If any check fails, the pull request opens as a draft. A draft cannot be merged by accident. When the data was not pushed, the first line of the pull request says "Data was not pushed to the public repository because checks failed."

### When the job opens no pull request

The job needs the day's price sheet and an earlier sheet to build the price page. If either sheet is missing, the job opens no pull request and pushes nothing. The same happens when the GitHub CLI cannot list or open pull requests, for example because its login has expired. The job then does these things.

1. It writes the report, with host and account details removed, to `harness-daily-YYYY-MM-DD.log` in `HARNESS_DAILY_LOG_DIR`, or in the system temp directory when that key is not set.
2. It shows a macOS notification that names the log file.
3. It exits with code 1.

If the GPU step did not run (`--skip-gpu`, or the step stopped with an error), the job records the FAIL check `gpu step` and still opens a draft pull request with the price page.

### Running the job again on the same day

A second run on the same day is safe. Before it changes anything, the job looks for the day's pull request on the site repository. If the pull request exists, the job returns it and changes nothing.

If the branch `daily-YYYY-MM-DD` exists on the site repository but has no pull request, an earlier run stopped part way. The job opens a draft pull request from that branch as it is. The FAIL check `site checks` says that the site checks were not run again.

The data commit is also safe to repeat. The job makes no new commit when the day's files are already committed and unchanged. It fetches `origin` and makes no push when `origin/main` already holds the commit.

### Pull request text

The whole pull request text goes through the scrub in `harness/scrub.py` before it leaves the machine. This includes the title, the summary, the notes from the GPU step, and every check detail. The job then scans the text with the same privacy patterns it uses on the site files. Each line that still matches is replaced with `<removed>`, and the FAIL check `PR body privacy` drafts the pull request.

### Time limits and the lock

Each command the publish step runs stops after 900 seconds. `npm ci` and `npm test` get 1800 seconds. The check that the harness commit is public stops after 30 seconds.

The lock file holds the process ID of the run that owns it. A new run takes over a lock only when the lock is older than 6 hours and its process is no longer running. A run removes the lock at the end only if the lock still holds its own process ID.

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

### FLAG checks

A FLAG does not draft the pull request. It asks a person to look before merging.

- `price changes`. A provider changed an input or output price since the previous sheet. The detail lists each change.
- `speed band`. Output tokens per second moved more than 2 percent from the mean of the earlier days.
- `token band`. Output tokens per run moved more than 5 percent from the mean of the earlier days.

On the first day for a model there is nothing to compare, and both band checks pass.

## Environment keys

The job reads its settings from an env file outside the repository. Nothing in the repository holds a host name, an account name, or a secret. The `HARNESS_DAILY_ENV` key holds the path of the env file.

The push command is not a setting. The job always pushes with `git push origin main`, and only when this repository is on the `main` branch. If it is on another branch, the FAIL check `harness push` drafts the pull request.

| Key | What it holds |
|---|---|
| `HARNESS_GPU_SSH` | The ssh target of the GPU machine. |
| `HARNESS_GPU_WORKDIR` | The directory on the GPU machine where runs happen. |
| `HARNESS_GPU_ENV_PATH` | The path on the GPU machine of the Python environment with vLLM. |
| `HARNESS_GPU_HF_HOME` | The model cache directory on the GPU machine. |
| `HARNESS_GPU_INDEX` | The index of the one GPU the runs use. |
| `HARNESS_SITE_REPO_URL` | The clone URL of the site repository. |
| `HARNESS_SITE_WORKDIR` | The local directory where the job keeps its clone of the site repository. |
| `HARNESS_GH_CONFIG_DIR` | The GitHub CLI config directory whose login opens the pull request. |
| `HARNESS_DAILY_ENV` | The path of the env file itself. |
| `HARNESS_DAILY_LOCK_DIR` | The directory for the lock that stops two runs at once. |
| `HARNESS_DAILY_LOG_DIR` | The directory for the local report when the job opens no pull request. The default is the system temp directory. |
| `HARNESS_DRY_RUN` | `1`, `true`, `yes` or `on`, in any case, turns on dry run. The GPU step lists its commands and runs none. The publish step makes no commit, no push, no clone and no pull request, and prints what it would do. Any other value, or no value, is a normal run. |
| `HARNESS_PRIVATE_TERMS` | Comma-separated words that must never appear in a result file, for example a host name. The privacy check fails if one does. |

## Run it by hand

Run a dry run from the repository root.

```
python3 -m harness.daily --dry-run
```

`--dry-run` runs no step at all. It prints the steps that would run. A dry run is the safe first test of a new env file. To run the steps without a push or a pull request, set `HARNESS_DRY_RUN=1` instead. Run without either to do the full job once.

## Install the schedule

The schedule is a macOS LaunchAgent. It runs `python3 -m harness.daily` once a day in your login session, so the keychain holds the ssh key the job needs. The files are in `scripts/launchd/`.

1. Write the env file outside the repository, with the keys above.
2. Run the installer with your own paths. The hour and minute are optional and default to 6 and 0.

```
scripts/launchd/install.sh --python /path/to/python3 --repo /path/to/this/repo \
  --logdir /path/to/logs --envfile /path/to/daily.env --hour 6 --minute 30
```

3. The installer writes `~/Library/LaunchAgents/net.starfield.harness-daily.plist` and prints a `launchctl bootstrap` command. The installer does not run that command. Run it yourself to load the job.

Standard output and standard error go to two files in the log directory. To remove the job, run `scripts/launchd/install.sh --uninstall`. It prints the `launchctl bootout` command and the file to delete.

## Add a model

Edit `config/models.json`. Put a model in `core` to run it every day, or in `rotating` for the rotating set. Each entry has an `id`, a pinned `revision`, and a `thinking_off` flag.

Check these once before you add the entry.

- The licence allows running the model and publishing the speed numbers.
- The weights fit on one 32 GB card in fp16, with room for the cache.
- The model loads on the pinned vLLM.
- Thinking is off where the model has a thinking mode, and `thinking_off` is `true`.
- The entry pins a revision, as a full commit hash.

A model that needs more than one GPU is out of scope for this job. The `one GPU` check fails on it.
