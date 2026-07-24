# Contributing

Thanks for helping improve Nitro AI Judge CLI.

## Before You Start

- Use GitHub Issues for reproducible bugs and focused feature requests.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development Setup

NAIJ requires Python 3.10 or newer and has no runtime dependencies.

```bash
git clone https://github.com/MihneaTeodorStoica/nitro-ai-judge-cli.git
cd nitro-ai-judge-cli
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
```

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

## Pull Requests

- Keep each change focused and avoid unrelated refactors.
- Add or update tests when behavior changes.
- Preserve the canonical `naij` command and the deprecated `nitro-cli`
  compatibility entry point.
- Run the complete test suite before submitting.
- Explain the problem, the chosen fix, and any user-visible changes in the
  pull request.

By contributing, you agree that your contributions will be licensed under the
repository's [MIT License](LICENSE).
