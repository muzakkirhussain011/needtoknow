# Contributing

Thanks for taking the time. Bug reports, policy ideas and pull requests are all welcome.

## Set up

```bash
git clone https://github.com/muzakkirhussain011/rampart-ai-gateway.git
cd rampart-ai-gateway
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Policy tests need the [OPA CLI](https://www.openpolicyagent.org/docs/latest/#running-opa):
`opa test policies -v`. Integration tests need Docker:

```bash
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
```

No model or GPU is needed for any test; the unit tests use a fake model and a deterministic
embedder.

## Pull requests

- Keep each pull request to one change, with a test that fails without it.
- Security-relevant behaviour is tested as a property ("a denied request never reaches the
  model"), not only as a happy path.
- **Policy changes** (`policies/`) need Rego tests; CI fails below 90% policy coverage. Explain in
  the description who gains or loses access.
- `ruff check .`, `ruff format --check .`, `opa fmt --list --fail policies` and the tests must pass.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `build:`, `chore:`).
- Add a line to `CHANGELOG.md` under *Unreleased* for anything a user would notice.

## Design changes

For a change to how access control, PII handling or the audit trail works, open an issue first.
Significant decisions are recorded in `docs/adr/`.
