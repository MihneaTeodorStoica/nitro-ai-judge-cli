# Nitro AI Judge CLI

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/nitro-ai-judge-cli?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/nitro-ai-judge-cli)

`naij` is a command-line client for `judge.nitro-ai.org`. It can browse competitions and tasks, download task data, submit solutions, inspect feedback, and launch past competitions locally with Docker.

For a full-screen contest cockpit with searchable contests, ordered tasks, data
downloads, local play controls, and submission feedback, run:

```bash
naij tui
```

The TUI can start before login and will prompt for credentials when needed.
Selections are shared with the regular `naij` shell and commands.

## 3.0 highlights

- `naij tui` provides a keyboard-first, full-screen contest cockpit.
- `naij play manager install` starts the local container-backed Play manager; its
  dashboard updates live when NAIJ or the container runtime changes a managed competition.
- The manager keeps Play services private behind stable Jupyter and submission
  proxy routes, with operation progress, redacted logs, cancellation, and
  offline recovery instructions.
- **Disconnect Nitro** removes only credentials saved inside the manager.
  LAN-only **Log out** expires only the current browser session. Neither action
  removes CLI credentials or workspaces.

## Requirements

- Python 3.10+
- Textual 8.2.x (installed automatically)
- Podman with Compose (preferred when installed), or a local Linux-container
  Docker daemon with the Compose plugin, for `naij play`

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
secret-tool lookup service naij | naij login --username USER --password-stdin
naij logout
naij --version
```

Login posts to Nitro's `/api/auth/login` endpoint and stores the returned access and refresh tokens. Nitro AI Judge refreshes an expired access token when possible and asks you to log in again when the saved refresh token is no longer valid.
`--password-stdin` accepts one password line from a pipe and refuses an interactive
terminal. `naij logout` removes only CLI credentials; Play manager credentials,
context, cache, history, and workspaces remain unchanged.

## Full-screen TUI

Run `naij tui` for the full-screen interface. Its Yazi-inspired browser keeps
contests and one-based tasks in separate columns while the right pane provides
four task views:

1. Overview
2. Data
3. Submissions
4. Play

The layout adapts to the terminal. At 100 columns and wider all three panes
remain visible. From 60 through 99 columns, only the active pane is shown and
Tab/Shift-Tab cycles contests, tasks, and task details. Below 60 columns or 20
rows, Nitro AI Judge shows a resize message instead of compressing the interface.

The complete keymap is:

- `j`/`k` or Up/Down: move
- Enter: open
- `h`/`l` or Left/Right: move across browser panes and task views
- Esc: back
- Tab/Shift-Tab: cycle panes, move between form fields, and in Submissions toggle
  between the submission list and the detail scroller
- `/`: filter the active contest, task, or submission list
- `r`: refresh the active view
- `?`: contextual help
- `q` or Ctrl+D: quit
- `1`–`4`: select Overview, Data, Submissions, or Play
- `d`: open the task-data download form
- `s`: open the submission form
- `p`: open the Play action menu

Forms use Space to toggle choices and Tab to move; the submission form also has
clickable Submit and Cancel buttons, and the Submissions view has a persistent
New submission button. That view requests and displays only the signed-in
user's submissions. Destructive Play actions require confirmation. Mouse
selection, scrolling, tab switching, and field focus are supported as an
optional convenience; every workflow remains keyboard-accessible. Hold Shift
while dragging when the terminal's normal text selection is needed.

The TUI loads cached contests, tasks, and submissions before connecting. A
temporary network failure or expired login leaves that cached content visible
with one recovery message at the bottom. When a request detects stale
credentials, the TUI refreshes the bearer token and generated site cookie once,
retries once, and opens the login form if recovery fails. A real `403` after
refresh remains an access-denied error rather than reopening login repeatedly.
Loaded task statements are merged into the task cache, so reopening a task can
show its full statement before the next network refresh.

Use `naij ls --offline` or `naij show --offline` to browse the selected cached
scope without authentication or network access. Cached output is marked and
reports that freshness is unavailable. Inspect or invalidate stale entries with
`naij cache status` and `naij cache clear [contests|tasks|submissions|all]`;
clearing cache never changes credentials or the current selection.

Release screenshots and the keyboard walkthrough will be added under
`docs/assets/` with the `v3.0.0` release capture; the recording contract is in
[`docs/assets/README.md`](docs/assets/README.md).

### Help wanted: TUI improvements

Contributions to the TUI are welcome, especially improvements to interaction
polish, visual clarity, accessibility, and behavior across terminal sizes.
Please open an issue before starting a large redesign, preserve keyboard access
and existing CLI behavior, and include a focused Pilot test for behavior
changes. See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.

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

If saved context is missing or stale, Nitro AI Judge reports which `naij use` command will refresh it.

## Downloads and submissions

Download one or more task-file categories with `-c`/`--category`. Supported categories are `statement`, `train_data`, `test_data`, `sample_output`, and `custom_archive`.

```bash
naij download-data --list
naij download-data -c statement -o TASK.md
naij download-data -c train_data -c test_data -d data
naij download-data -c test_data -o test_data.zip -f
```

Download options:

- `--list`: show available canonical category keys and labels without writing files
- `-c`, `--category`: category to download; repeat for multiple categories
- `-d`, `--out-dir`: destination directory
- `-o`, `--output`: destination file when downloading one category
- `-f`, `--force`: overwrite existing files

Submit options:

- `-o`, `--output`: required submission output file
- `-s`, `--source`: optional source file
- `-n`, `--note`: submission note
- `-w`, `--wait`: wait for judging feedback
- `--wait-timeout`: maximum wait in seconds (default: 180)

Submission-list options:

- `-a`, `--author`: filter by author
- `-p`, `--page`: page number
- `-n`, `--page-size`: results per page
- `-m`, `--mode`: `partial`, `complete`, or `both`

Use `naij set-final [SUBMISSION]` and `naij unset-final [SUBMISSION]` to change the final selection. The submission argument may be omitted when one is selected in the interactive shell.

Resume waiting after an interrupted submission with `naij submission [SUBMISSION] --wait`; the ID may be omitted when a submission is selected.

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

Version 3 routes every competition operation through one Dockerized Play
manager at <http://localhost:51123/nitro/>. The manager uses the host Docker
socket and Compose plugin; it does not run Docker-in-Docker. Competition
Jupyter and proxy ports stay private, while their browser routes remain stable
under the manager URL.

Install the manager once, then start a competition:

```bash
naij play manager install --yes
naij play algolymp/algolymp-preojia-ix-x
naij play status algolymp/algolymp-preojia-ix-x
naij play cancel algolymp/algolymp-preojia-ix-x
naij play logs algolymp/algolymp-preojia-ix-x
naij play logs -f algolymp/algolymp-preojia-ix-x
naij play stop algolymp/algolymp-preojia-ix-x
naij play start algolymp/algolymp-preojia-ix-x
naij play restart algolymp/algolymp-preojia-ix-x
naij play recreate algolymp/algolymp-preojia-ix-x
naij play delete-container algolymp/algolymp-preojia-ix-x
naij play delete-image algolymp/algolymp-preojia-ix-x
naij play delete-workspace algolymp/algolymp-preojia-ix-x --force
```

The competition can be omitted when saved context supplies it. `naij play
ORG/COMP` means `play play ORG/COMP`; the old `up` and `down` spellings remain
parseable as migration aliases.

Lifecycle behavior:

- `play`, `pull`, `start`, `stop`, `restart`, and `recreate` are idempotent,
  asynchronous manager operations. Identical concurrent requests share one
  operation; conflicting requests report `competition_busy`. Image pulls have
  no fixed deadline; `--wait-timeout` bounds service startup after a pull.
- Explicit `stop` remains stopped until the user starts or recreates it.
- `delete-container` removes containers and the private project network but
  preserves `/home/jovyan`.
- `delete-image` removes that competition's cached image tags and clears its
  fallback selection after containers are deleted; shared fallback images and
  workspace data are preserved.
- `delete-workspace` requires the full `organization/competition` reference in
  interactive use. Automation must pass `--force`.
- `logs` is redacted before it leaves the manager; `logs --follow` streams it.
- `cancel` requests cancellation of the latest queued or running operation.
- `--gpu` requires GPU access, `--no-gpu` disables it, and the default probes
  automatically. `--pull always|missing|never` retains Compose-style policy.
- `--open` opens Jupyter after a successful `play` or `recreate`; commands do
  not open a browser by default.
- Environments saved by 3.0.2 keep their existing images on `start` and
  `restart`. Their next `pull`, `play`, or `recreate` updates Compose state to
  the current shared fallback images when contest-specific images are absent.
  With `--pull never`, those exact fallback digests must already be cached;
  otherwise the command fails without changing the saved environment.

The retired competition `--port`, `--proxy-port`, and `--bind` flags produce
guidance instead of publishing a container port. Configure the single manager
endpoint with `naij play manager install --bind ... --port ...`.

Manager lifecycle commands are:

```bash
naij play manager status
naij play manager update --yes
naij play manager open
naij play manager stop
naij play manager start
naij play manager restart
naij play manager sync-credentials
naij play manager uninstall
```

NAIJ automatically pulls and recreates an installed older official manager
before the next Play command, TUI launch, `manager install`, `manager start`, or
`manager restart`. Endpoint, TLS, private manager state, competition
workspaces, and custom manager images are preserved.

Uninstall removes the manager container and network but preserves its private
SQLite volume, configuration, competition metadata, images, and every
workspace. `manager purge --force` removes only manager-private SQLite state;
it never removes competition workspaces.

### LAN access and TLS

Loopback is the safe default. A non-loopback bind is rejected unless all three
of `--tls-cert`, `--tls-key`, and an HTTPS `--public-url` are supplied. LAN mode
uses a separate dashboard login token at
`~/.naij/play-manager/dashboard-login-token`, rate-limits login, and still
requires the private CLI API credential for CLI calls:

```bash
naij play manager install --yes \
  --bind 0.0.0.0 --port 51123 \
  --tls-cert /absolute/path/cert.pem \
  --tls-key /absolute/path/key.pem \
  --public-url https://play.example.test:51123
```

The dashboard uses strict Host and Origin validation, SameSite sessions, CSRF,
CSP, and clickjacking protection. Access and refresh tokens are synchronized
through the authenticated API after install, login, or refresh and never
appear in URLs, Compose values, logs, or dashboard HTML.

### Migration and recovery

Installation examines only known 2.x generated state and exact Compose labels,
then sends a sanitized adoption manifest. The first action lazily cuts over a
verified legacy environment. An unlabeled named workspace is reused without
alteration; a container-layer workspace is copied into a labeled volume while
the stopped legacy container remains recovery evidence until the new routes
pass validation. Failed cutover removes only newly manager-labeled objects and
restarts the old container.

An update retains the previous image and generated configuration. If the new
manager does not become healthy, the CLI restores and restarts the previous
configuration. See [the architecture document](docs/play-manager.md) for the
runtime model, labels, API, and recovery boundaries.

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

Generate completion for zsh, bash, fish, or PowerShell:

```bash
naij completion zsh
naij completion bash
naij completion fish
naij completion powershell
```

For the current shell session:

```bash
# zsh, after compinit
source <(naij completion zsh)

# bash
source <(naij completion bash)

# fish
naij completion fish | source

# PowerShell
naij completion powershell | Out-String | Invoke-Expression
```

Completion resolves the current argument slot and shows only its next useful level. It does not mix options with an available contest, task, or submission; typing `-` switches immediately to remaining valid options. It lazily fetches a missing entity list on the first relevant Tab and saves it in the context cache. It fetches all competitions, only the selected or supplied competition's tasks, or the current user's partial and complete submissions for the selected task. Existing cache entries, including empty lists, suppress later requests; authentication and network failures stay silent and can be retried on a later Tab. Bare native completion remains command-only, while a blank interactive prompt offers only entities at the current context level.

## State and security

By default, Nitro AI Judge stores credentials, context, shell history, and play data under `~/.naij/`:

```text
~/.naij/state.json
~/.naij/context.json
~/.naij/history
~/.naij/play-manager/manager.json
~/.naij/play-manager/compose.json
~/.naij/play-manager/cli-api-token
~/.naij/contestant-cloud/        # read-only 2.x migration source
```

Override the root with:

```bash
NAIJ_STATE_DIR=/some/path naij login
naij --state-dir /some/path login
```

State-directory precedence is `--state-dir`, `NAIJ_STATE_DIR`, the 2.x
`NITRO_STATE_DIR` fallback, then `~/.naij`.

The state directory is mode `0700`; credential, context, and history files are mode `0600`. Writes use a same-directory temporary file, `fsync`, and atomic replacement. Corrupt credentials are left untouched and produce a recovery-oriented login error. Corrupt context is ignored with a warning.

## Command compatibility

`naij` remains canonical. The deprecated `nitro-cli` executable delegates to
the same entry point and now remains available until 4.0.0. There is no legacy
Python import package.

The legacy `NITRO_STATE_DIR`, `NITRO_API_BASE_URL`, and
`NITRO_SUBMISSION_PROXY` variables remain lower-priority compatibility
fallbacks for the 3.x transition.

Default state moves from `~/.nitro-cli` to `~/.naij`. If only the old directory exists, Nitro AI Judge renames it before loading state. If both exist, Nitro AI Judge uses the new directory, leaves the old one untouched, and warns once per process. If the rename fails, Nitro AI Judge uses the old directory for that run and prints manual-migration guidance. Setting either state-directory environment variable disables automatic default-path migration.

Generated native completion registers both command names while invoking `naij`
internally.

## Publishing

The release workflow runs host tests on Linux, Windows, and macOS, builds and
checks the CLI, builds the manager, and runs Linux Docker integration. A release
tag must exactly match `pyproject.toml`. It refuses an existing immutable GHCR
tag, publishes `linux/amd64` and `linux/arm64`, then updates the matching `3.0`
and `stable` tags. PyPI publication happens only after GHCR succeeds. The
development branch publishes `edge`; neither the workflow nor installer uses
`latest`.

Recommended release checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine check dist/*
docker build -f manager/Dockerfile -t naij-play-manager:dev .
NAIJ_DOCKER_INTEGRATION=1 NAIJ_PLAY_MANAGER_IMAGE=naij-play-manager:dev \
  python3 -m unittest discover -s tests/integration -v
git tag v3.1.5
git push origin main --tags
```

PyPI project: `nitro-ai-judge-cli`

PyPI does not allow re-uploading an existing filename, and GHCR exact version
tags are treated as immutable. A new release requires a new version and matching
tag. Trusted publishing targets this repository and the `pypi` environment.
