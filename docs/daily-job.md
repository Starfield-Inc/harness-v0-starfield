# The daily job

The daily job reads prices, measures the models on one GPU, checks the results, and opens a pull request on the site repository. A person reviews the pull request. The job never merges and never deploys.

## What the job does each day

The job runs these steps in order.

1. It reads the price pages and writes the day's price sheet.
2. It runs every model in `config/models.json` on one GPU with the pinned vLLM, using the fixed prompt set.
3. It runs the checks listed below on the price sheet and on the run files.
4. It commits the new data to this repository and pushes the commit.
5. It opens a pull request on the site repository with the new data and a table of the check results.

If any check fails, the pull request opens as a draft. A draft cannot be merged by accident.

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
| `HARNESS_PUSH_CMD` | The command that pushes the data commit. |
| `HARNESS_DAILY_ENV` | The path of the env file itself. |
| `HARNESS_DAILY_LOCK_DIR` | The directory for the lock that stops two runs at once. |
| `HARNESS_DRY_RUN` | When set, the job makes no push and opens no pull request. |

## Run it by hand

Run a dry run from the repository root.

```
python3 -m harness.daily --dry-run
```

A dry run is the safe first test of a new env file. Run without `--dry-run` to do the full job once.

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
