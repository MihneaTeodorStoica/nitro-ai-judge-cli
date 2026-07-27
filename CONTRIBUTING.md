# Contributing

Thanks for helping improve Nitro AI Judge CLI.

## Before You Start

- Use GitHub Issues for reproducible bugs and focused feature requests.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development Setup

NAIJ requires Python 3.10 or newer. Textual is a host dependency; the manager
image adds `aiohttp` through the `manager` extra.

```bash
git clone https://github.com/MihneaTeodorStoica/nitro-ai-judge-cli.git
cd nitro-ai-judge-cli
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable ".[manager]"
```

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

Build the manager and run the opt-in Linux Docker check with:

```bash
docker build -f manager/Dockerfile -t naij-play-manager:dev .
NAIJ_DOCKER_INTEGRATION=1 NAIJ_PLAY_MANAGER_IMAGE=naij-play-manager:dev \
  python -m unittest discover -s tests/integration -v
```

Do not run Docker integration against a machine with an existing Play manager;
the test skips rather than replacing one. Windows and macOS host unit tests run
in CI, but Docker Desktop behavior needs a real Desktop smoke before it can be
claimed as verified.

## Pull Requests

- Keep each change focused and avoid unrelated refactors.
- Add or update tests when behavior changes.
- Preserve the canonical `naij` command and the deprecated `nitro-cli`
  compatibility entry point.
- Keep competition Docker calls inside the manager backend. Host Play commands
  and the TUI must use the shared manager client.
- Never expose credentials in Compose values, URLs, logs, dashboard HTML, or
  test fixtures.
- Run the complete test suite before submitting.
- Explain the problem, the chosen fix, and any user-visible changes in the
  pull request.

By contributing, you agree that your contributions will be licensed under the
repository's [MIT License](LICENSE).
