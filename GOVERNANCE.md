# Governance

wardowl is an open-source project under the MIT license. This document describes how it is run
today and how that changes as the community grows.

## Roles

- **Contributors**: anyone who opens an issue, reviews a pull request, improves documentation or
  submits code.
- **Maintainers**: contributors with merge rights. They review and merge pull requests, triage
  issues, cut releases and uphold the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Lead maintainer**: currently the project's author,
  [Syed Muzakkir Hussain](https://github.com/muzakkirhussain011). The lead maintainer has the
  final say when maintainers cannot reach consensus, and is responsible for releases and security
  response until there are enough maintainers to share them.

## How decisions are made

Most decisions happen in pull requests and issues by lazy consensus: a change that has been
reviewed and has no unresolved objections is merged. Changes to the public API, the security
guarantees or this document need an issue first and agreement from a maintainer other than the
author (once there is more than one maintainer). Significant design decisions are recorded in
[`docs/adr`](docs/adr).

## Becoming a maintainer

Contributors who have made several substantial, high-quality contributions (code, reviews or
documentation) over at least two months, and who know the project's security guarantees well, may
be invited by an existing maintainer. Maintainers who are inactive for six months may step back to
emeritus status and can return at any time.

## Changes to governance

This document will change as the project grows; for example, a steering group may replace the
lead-maintainer role once there are several active maintainers. Changes to governance are made by
pull request and announced in the changelog.
