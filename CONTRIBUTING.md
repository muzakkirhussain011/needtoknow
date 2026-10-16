# Contributing to needtoknow

Thank you for helping. Bug reports, adapter ideas, documentation fixes and pull requests are all
welcome, from first-time contributors too. Look for issues labelled **good first issue**.

## Set up

```bash
git clone https://github.com/muzakkirhussain011/needtoknow.git
cd needtoknow
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

The core needs no services, no GPU and no network. Integration tests run against a real service
when its address is set (`NEEDTOKNOW_TEST_DATABASE_URL` for PostgreSQL with pgvector,
`NEEDTOKNOW_TEST_OPA_URL`, `NEEDTOKNOW_TEST_OPENFGA_URL`) and are skipped otherwise. CI runs all of
them; [`.github/workflows/ci.yml`](.github/workflows/ci.yml) shows how each service is started.

## What makes a good pull request

- **One change per pull request**, with a test that fails without it.
- **Security behaviour is tested as a property, not an example.** If you add a backend or an
  authorizer, add it to the property-based suite (see `tests/test_properties.py`): for random
  corpora, permissions, subjects and queries it must never return a chunk the subject may not
  read, and it must agree with the in-memory reference implementation.
- **Fail closed.** An authorizer that cannot decide raises `AuthorizationError`. Never return
  "allow" on errors, timeouts or unexpected responses.
- **No new core dependencies.** Integrations with external systems are optional extras
  (`pip install "needtoknow[pgvector]"`), imported only inside their own module.
- **Public API changes** need an issue first. After 1.0 the public API follows semantic
  versioning: no breaking changes without a deprecation period.
- Add a line to `CHANGELOG.md` under *Unreleased* for anything a user would notice.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`, `chore:`).

## Adding an adapter

A backend implements `search(query, k, filter=None) -> list[Hit]` and sets `supports_filters`.
Translate the `In` / `And` / `Or` filter into the store's native filter; the in-memory backend and
`needtoknow.filters.matches` define the semantics you must match, including "a missing field
never matches". An authorizer implements `check(subject, hits, action)` and, when the permission
system can enumerate what a subject may read, `filter_for(subject, action)`.

## Design changes

Significant decisions are recorded as architecture decision records in [`docs/adr`](docs/adr).
Propose larger changes in an issue or discussion first, so that design questions are settled
before code is written.

## Releasing

Maintainers release from `main`:

1. Open a pull request that sets `__version__` in `needtoknow/__init__.py` and moves the
   *Unreleased* changelog entries under a dated heading for the new version.
2. After it merges, publish a GitHub release tagged `v` plus the version (for example `v1.2.0`),
   marked as a pre-release for release candidates.
3. [`release.yml`](.github/workflows/release.yml) builds the package, checks that the tag matches the
   version, and uploads it to PyPI with trusted publishing; no API token exists.
   [`verify-release.yml`](.github/workflows/verify-release.yml) then installs it from PyPI and runs
   the quickstart against it.

Versions follow [semantic versioning](https://semver.org/). Every Python version that CPython
still supports is supported; a version is dropped in the first minor release after its end of life.

## Conduct and governance

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). How decisions are made and
how contributors become maintainers is described in [GOVERNANCE.md](GOVERNANCE.md).

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
