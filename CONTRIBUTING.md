# Contributing to Atlas

Thank you for helping improve Atlas. Bug reports, fixes and new provider adapters are welcome.

## Before you start

- Atlas measures every network with the same method. Changes that would treat one network differently from the others will not be accepted.
- Changes to scoring or measurement methods change published results. Please describe the effect on the rankings in your pull request, so it can be reviewed and documented in the methodology.
- Never commit credentials. Provider accounts belong in `.secrets/credentials.toml`, which Git ignores.

## Development setup

Requirements: Python 3.12 or newer with [uv](https://docs.astral.sh/uv/), and Go 1.27 or newer for the API.

```sh
uv sync --locked
uv run pytest

cd api
go test ./...
```

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request to one change, with tests for new behaviour and for every bug fix.
3. Run the Python and Go tests before you open the pull request.
4. Describe what changed and why, in plain words.

## Adding a provider

Add a run config in `configs/<provider>.toml` and a credential grammar in `src/atlas/providers/grammars/<provider>.toml`, following the existing ones and the [provider guide](docs/providers.html). Adding a provider to the framework does not add it to the published Atlas rankings; that is decided by the maintainers.

## Reporting security issues

Please do not open a public issue or pull request for a security problem. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
