# Nitro CLI

CLI client for `judge.nitro-ai.org`.

## Features

- login using Nitro's JSON API
- list contests with page controls
- list tasks for a contest
- view full task statements
- download task data files
- submit solutions and wait for feedback
- list submissions
- inspect submission feedback/details
- interactive shell mode when run without arguments

## Requirements

- Python 3.10+
- no third-party Python package dependencies

## Login

`nitro-cli login` posts to Nitro's `/api/auth/login` endpoint and stores the returned access and refresh tokens.

Behavior:

1. saves API tokens in the local CLI state file
2. refreshes an expired access token when a refresh token is available
3. asks you to log in again if the saved refresh token is no longer valid

Example:

```bash
nitro-cli login --username MihneaStoica --password '...'
```

## Installation

With `pipx`:

```bash
pipx install .
```

With `pip`:

```bash
python3 -m pip install .
```

For local development:

```bash
python3 -m pip install -e .
```

From PyPI:

```bash
pipx install nitro-ai-judge-cli
```

## Usage

Direct commands:

```bash
nitro-cli login
nitro-cli contests
nitro-cli contests --page 2
nitro-cli contests --all-pages
nitro-cli tasks algolymp/algolymp-preojia-ix-x
nitro-cli task algolymp/algolymp-preojia-ix-x 1
nitro-cli download-data algolymp/algolymp-preojia-ix-x 1 --out-dir data
nitro-cli download-data algolymp/algolymp-preojia-ix-x 1 --category test_data --output test_data.zip
nitro-cli submissions algolymp/algolymp-preojia-ix-x 1 --mode both
nitro-cli submission 3a009d767bd5 --org algolymp --comp algolymp-preojia-ix-x --task-id 1
nitro-cli submit algolymp/algolymp-preojia-ix-x 1 --output submission.csv --source solution.py --wait
```

Interactive shell:

```bash
nitro-cli
```

Example shell session:

```text
contest list
contest list --page 2
contest list --all-pages
select 20
tasks
select 1
show
task download-data --out-dir data
submit submission.csv solution.py --wait
submissions
submission 1
```

Shell commands:

```text
help
exit | quit
back | unselect
login [username] [password]
status
contests
contest list [--all] [--page N] [--page-size N] [--all-pages]
contest select <index|org/slug>
contest show
tasks
task list
task select <index|id>
select <index|id>
show
task download-data [--category CATEGORY ...] [--out-dir DIR] [--force]
submit <output.csv> [source.py] [--note TEXT] [--wait]
task show
task submit <output.csv> [source.py] [--note TEXT] [--wait]
submissions [--mode partial|complete|both]
task submissions list [--mode partial|complete|both]
submission <index|short-id|full-id>
submission view <index|short-id|full-id>
task submissions show <index|short-id|full-id>
set-final <index|short-id|full-id>
unset-final <index|short-id|full-id>
```

Notes:

- `select` is context-sensitive: at top level it selects a contest; inside a contest it selects a task.
- `back` / `unselect` clears the current task first, then the current contest.

## State

Login state is stored in:

```text
~/.nitro-cli/state.json
```

Override with:

```bash
NITRO_STATE_DIR=/some/path nitro-cli login
```

## Publishing

This repo includes a GitHub Actions workflow at `.github/workflows/publish.yml`.

Behavior:

1. Builds the package on pushes, pull requests, and manual runs
2. Runs `twine check` on built distributions
3. Publishes to PyPI when you push a tag matching `v*`

Recommended release flow:

```bash
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine check dist/*
# bump version in pyproject.toml before tagging a new release
git tag v0.1.3
git push origin main --tags
```

PyPI does not allow re-uploading the same filename for an existing release. The workflow is configured to skip already-published files on reruns, but publishing a new release still requires a new version in `pyproject.toml` and a matching new tag.

For PyPI trusted publishing, configure PyPI to trust this GitHub repository and the `pypi` environment.

Package name on PyPI: `nitro-ai-judge-cli`
Command name after install: `nitro-cli`

Before publishing publicly, choose and add a license file.
