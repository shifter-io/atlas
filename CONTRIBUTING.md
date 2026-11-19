# Contributing to Atlas

Thank you for helping improve Atlas. Pull requests with fixes and new provider adapters are welcome. This repository does not take issues yet; security reports go through private vulnerability reporting (see [SECURITY.md](SECURITY.md)).

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

### What happens to a pull request from a fork

Two required checks, `tier1` and `api`, run only on the maintainers' own CI servers, never for a fork. On a fork's pull request they show as failed within seconds, by design. The result to watch is `public`, which runs the tests on GitHub's own runners. A maintainer may have to approve that run first.

A maintainer then reviews the change and re-creates it on a branch of this repository as one commit with a `Co-authored-by` line naming you, so GitHub credits you. That pull request goes through the full checks and the merge queue, and your pull request is closed with a link to it. [docs/plans/public-ci.md](docs/plans/public-ci.md) ("Outside pull requests") describes this.

## Adding a provider

Add a run config in `configs/<provider>.toml` and a credential grammar in `src/atlas/providers/grammars/<provider>.toml`, following the existing ones and the [provider guide](docs/providers.html). Adding a provider to the framework does not add it to the published Atlas rankings; that is decided by the maintainers.

## Reporting security issues

Please do not open a public issue or pull request for a security problem. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
