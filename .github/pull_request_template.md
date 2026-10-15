## What and why

## How it was tested

## Checklist

- [ ] Tests added or updated; security behaviour tested as a property, not only the happy path
- [ ] New backends or authorizers are in the property-based suite and fail closed on errors
- [ ] No new core dependencies (integrations go in optional extras)
- [ ] `ruff check .`, `ruff format --check .` and `pytest` pass
- [ ] `CHANGELOG.md` updated under *Unreleased* if users would notice
