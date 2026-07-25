# Nitro AI Judge CLI (NAIJ)

[![PyPI downloads](https://img.shields.io/pypi/dm/nitro-ai-judge-cli?label=PyPI%20downloads)](https://pypi.org/project/nitro-ai-judge-cli/)

`naij` is a command-line client for `judge.nitro-ai.org`. It can browse competitions and tasks, download task data, submit solutions, inspect feedback, and launch past competitions locally with Docker.

> **Help wanted:** A new Textual TUI is under active development. Testing,
> UI/UX feedback, bug reports, and code contributions are welcome on the
> [`feat/textual-tui-usability-overhaul`](https://github.com/MihneaTeodorStoica/nitro-ai-judge-cli/tree/feat/textual-tui-usability-overhaul)
> branch.

## Requirements

- Python 3.10+
- no third-party runtime dependencies
- Docker with `docker compose` for `naij play`

## Installation

From PyPI:

```bash
pipx install nitro-ai-judge-cli
```

On Arch Linux:
available on AUR as `nitro-ai-judge-cli`
```bash
yay -S nitro-ai-judge-cli
```

From a checkout:

```bash
python3 -m pip install .
```

For local development:

```bash
python3 -m pip install -e .
```

## Login

```bash
naij login
naij login --username MihneaStoica
```

Login posts to Nitro's `/api/auth/login` endpoint and stores the returned access and refresh tokens. NAIJ refreshes an expired access token when possible and asks you to log in again when the saved refresh token is no longer valid.

## Basic usage

All commands still accept explicit competition and task arguments:

```bash
naij contests
naij contests --page 2
naij contests --all-pages
naij tasks algolymp/algolymp-preojia-ix-x
naij task algolymp/algolymp-preojia-ix-x 1
naij download-data algolymp/algolymp-preojia-ix-x 1 -d data
naij submissions algolymp/algolymp-preojia-ix-x 1 -m both
naij submission 3a009d767bd5 --org algolymp --comp algolymp-preojia-ix-x --task-id 1
naij submit algolymp/algolymp-preojia-ix-x 1 -o submission.csv -s solution.py -w
```

Competitions may be written as either `ORG/COMP` or `ORG COMP`. Explicit arguments override saved context for that invocation without changing it.

## Persistent context

`use` saves a competition and optional task for later commands:

```bash
naij use                         # show the current context
naij use algolymp/preojia        # select a competition
naij use algolymp/preojia 1      # select a competition and task
naij use 2                       # select a task in the current competition
naij use --clear                 # clear the selection
```

Changing the competition clears the selected task and submission. Changing the task clears the selected submission.

`ls` and `show` follow the current context:

- With no selection, `naij ls` lists competitions.
- With a competition selected, `naij ls` lists its tasks.
- With a task selected, `naij ls` lists its submissions.
- `naij show` displays the most specific selected competition, task, or submission.

Commands use saved context when their positional arguments are omitted:

```bash
naij tasks
naij task
naij download-data -d data
naij submissions -m both
naij submit -o submission.csv -s solution.py -w
naij submission
naij set-final
naij play
```

If saved context is missing or stale, NAIJ reports which `naij use` command will refresh it.

## Downloads and submissions

Download one or more task-file categories with `-c`/`--category`. Supported categories are `statement`, `train_data`, `test_data`, `sample_output`, and `custom_archive`.

```bash
naij download-data -c statement -o TASK.md
naij download-data -c train_data -c test_data -d data
naij download-data -c test_data -o test_data.zip -f
```

Download options:

- `-c`, `--category`: category to download; repeat for multiple categories
- `-d`, `--out-dir`: destination directory
- `-o`, `--output`: destination file when downloading one category
- `-f`, `--force`: overwrite existing files

Submit options:

- `-o`, `--output`: required submission output file
- `-s`, `--source`: optional source file
- `-n`, `--note`: submission note
- `-w`, `--wait`: wait for judging feedback

Submission-list options:

- `-a`, `--author`: filter by author
- `-p`, `--page`: page number
- `-n`, `--page-size`: results per page
- `-m`, `--mode`: `partial`, `complete`, or `both`

Use `naij set-final [SUBMISSION]` and `naij unset-final [SUBMISSION]` to change the final selection. The submission argument may be omitted when one is selected in the interactive shell.

## Backend and submission proxy

Use a different API endpoint with either a flag or an environment variable:

```bash
naij --api-url http://127.0.0.1:8080 tasks algolymp/preojia
NAIJ_API_BASE_URL=http://127.0.0.1:8080 naij tasks algolymp/preojia
```

API URL precedence is:

1. `--api-url`
2. `NAIJ_API_BASE_URL`
3. the 2.x `NITRO_API_BASE_URL` fallback
4. `PROXY_URL`
5. `https://judge.nitro-ai.org/api`

When `PROXY_URL` supplies the API URL, submission-proxy mode is enabled automatically. It can also be enabled explicitly:

```bash
PROXY_URL=http://127.0.0.1:8080 naij submit -o submission.csv -s solution.py
naij --api-url http://127.0.0.1:8080 --submission-proxy submit -o submission.csv -s solution.py
NAIJ_SUBMISSION_PROXY=1 naij submit -o submission.csv -s solution.py
```

Submission-proxy precedence is `--submission-proxy`, `NAIJ_SUBMISSION_PROXY`, the 2.x `NITRO_SUBMISSION_PROXY` fallback, implicit `PROXY_URL` mode, then disabled. Empty environment values are ignored; recognized true values are `1`, `true`, `yes`, and `on`.

## Past-contest play

`play` creates a persistent contest workspace with the competition's notebook and submission-proxy images:

```bash
naij play algolymp/algolymp-preojia-ix-x
naij play up algolymp/algolymp-preojia-ix-x --pull missing
naij play status algolymp/algolymp-preojia-ix-x
naij play logs algolymp/algolymp-preojia-ix-x
naij play logs -f algolymp/algolymp-preojia-ix-x
naij play stop algolymp/algolymp-preojia-ix-x
naij play restart algolymp/algolymp-preojia-ix-x
naij play down algolymp/algolymp-preojia-ix-x
naij play down algolymp/algolymp-preojia-ix-x --volumes --force
```

The competition can be omitted from every play action when context supplies it. `naij play ORG/COMP` is an alias for `play up`.

Lifecycle behavior:

- `play up` creates or recreates the services while reusing saved ports and workspace data.
- `play start`, `stop`, and `restart` match the corresponding Compose operations. `stop` keeps the containers.
- `play logs` prints existing logs; `-f`/`--follow` follows them.
- `play ps` shows Compose state; `play status` shows saved URLs, GPU mode, images, and workspace paths.
- `play down` removes containers and the contest network but preserves workspace data.
- `play down --volumes` deletes workspace data after confirmation. Non-interactive use also requires `--force`.

`play up` supports `--gpu`/`--no-gpu`, `--port`, `--proxy-port`, `--bind`, `--pull always|missing|never`, and `--wait-timeout`. Ports 8888 and 9000 are preferred and move upward automatically when unavailable. Services bind to `127.0.0.1` by default; use `--bind 0.0.0.0` only when LAN exposure is intended. GPU availability is detected automatically unless explicitly enabled or disabled.

Pull policy is evaluated before pulling:

- `always` pulls both images.
- `missing` pulls only images absent locally and is the default.
- `never` fails if an image is absent.

Each required pull displays `Pulling image N/TOTAL: IMAGE`, followed by one `Pulled image: IMAGE` line. Redirected output uses plain lines without terminal control sequences, and locally present images do not show a pull spinner under the `missing` policy.

Generated files live under `~/.naij/contestant-cloud/ORG-COMP/`. Each competition has its own Compose project, network, and persistent workspace volume mounted at `/home/jovyan`. Nitro image namespaces and product service identifiers remain unchanged.

## Interactive shell

Run `naij` without a command to start the shell. Its contest, task, and submission selection persists between sessions.

```text
$ naij
Nitro AI Judge Interactive Shell. Type `help` for commands.
[naij] > ls
[naij] > 2
[naij algolymp/preojia] > ls
[naij algolymp/preojia] > use 1
[naij algolymp/preojia 1] > show
[naij algolymp/preojia 1] > submit -o submission.csv -s solution.py -w
[naij algolymp/preojia 1] > ..
[naij algolymp/preojia] > q
```

Shell conveniences:

- `cd` is a shell-only alias for `use`; `cd ..`, `..`, `back`, and `unselect` move up one selection level.
- `pwd` prints the full current selection, `l` aliases `ls`, and `h` or `?` aliases `help` (including `h COMMAND`).
- `ls`, `use`, and `show` retain the same context-sensitive behavior as direct commands. Shell aliases are not top-level `naij` commands.
- A completed contest, task, or submission selects it directly; bare numbers remain available for indexed navigation.
- `help COMMAND` displays command-specific help.
- All regular commands work without a leading `naij`.
- Tab completes the current word; press it twice to list ambiguous matches. GNU Readline also supports forward/backward cycling with Tab/Shift-Tab.
- Completion is case-insensitive and slash-aware, and covers commands, options, enum values, competitions/tasks/submissions, and filesystem paths.
- Up/Down and Ctrl-P/Ctrl-N navigate history, Ctrl-R searches history, and Ctrl-L clears the screen. Home/End, Ctrl-A/Ctrl-E, Ctrl-Left/Ctrl-Right, Ctrl-W, Ctrl-U, and Ctrl-K provide standard line editing in GNU Readline and libedit.
- Ctrl-C cancels the current line, Ctrl-D exits, and invalid input returns to the prompt.

## Native completion

Generate completion for zsh, bash, or fish:

```bash
naij completion zsh
naij completion bash
naij completion fish
```

For the current shell session:

```bash
# zsh, after compinit
source <(naij completion zsh)

# bash
source <(naij completion bash)

# fish
naij completion fish | source
```

Completion resolves the current argument slot and shows only its next useful level. It does not mix options with an available contest, task, or submission; typing `-` switches immediately to remaining valid options. It lazily fetches a missing entity list on the first relevant Tab and saves it in the context cache. It fetches all competitions, only the selected or supplied competition's tasks, or the current user's partial and complete submissions for the selected task. Existing cache entries, including empty lists, suppress later requests; authentication and network failures stay silent and can be retried on a later Tab. Bare native completion remains command-only, while a blank interactive prompt offers only entities at the current context level.

## State and security

By default, NAIJ stores credentials, context, shell history, and play data under `~/.naij/`:

```text
~/.naij/state.json
~/.naij/context.json
~/.naij/history
~/.naij/contestant-cloud/
```

Override the root with:

```bash
NAIJ_STATE_DIR=/some/path naij login
naij --state-dir /some/path login
```

State-directory precedence is `--state-dir`, `NAIJ_STATE_DIR`, the 2.x
`NITRO_STATE_DIR` fallback, then `~/.naij`.

The state directory is mode `0700`; credential, context, and history files are mode `0600`. Writes use a same-directory temporary file, `fsync`, and atomic replacement. Corrupt credentials are left untouched and produce a recovery-oriented login error. Corrupt context is ignored with a warning.

## Migrating to 2.x

Version 2.x renames the canonical command from `nitro-cli` to `naij` and the Python package from `nitro_cli` to `nitro_ai_judge_cli`. The old `nitro-cli` command remains as a deprecated compatibility entrypoint throughout 2.x, prints a warning once per invocation, and will be removed in 3.0.0. There is no compatibility shim for importing `nitro_cli`.

The legacy `NITRO_STATE_DIR`, `NITRO_API_BASE_URL`, and `NITRO_SUBMISSION_PROXY` variables remain lower-priority fallbacks through 2.x and are planned for removal in 3.0.0.

Default state moves from `~/.nitro-cli` to `~/.naij`. If only the old directory exists, NAIJ renames it before loading state. If both exist, NAIJ uses the new directory, leaves the old one untouched, and warns once per process. If the rename fails, NAIJ uses the old directory for that run and prints manual-migration guidance. Setting either state-directory environment variable disables automatic default-path migration.

Generated native completion registers both command names during the 2.x compatibility period while invoking `naij` internally.

## Publishing

The workflow in `.github/workflows/publish.yml` runs the unittest suite on Python 3.10, 3.11, and 3.12, builds the distributions, runs `twine check`, and smoke-tests the installed wheel. Tags matching `v*` publish to PyPI only after tests and build validation pass.

Recommended release checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine check dist/*
git tag v2.1.0
git push origin main --tags
```

PyPI project: `nitro-ai-judge-cli`

PyPI does not allow re-uploading the same filename for an existing release. A new release requires a new version in `pyproject.toml` and a matching new tag. Trusted publishing should target this repository and the `pypi` environment.

Before publishing publicly, choose and add a license file.
