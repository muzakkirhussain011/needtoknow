# Security policy

needtoknow decides what data people may see, so security reports are taken seriously and are very
welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security > Report a vulnerability** on this repository.
Do not open a public issue for anything that could let a subject receive a chunk they may not
read, make an authorizer failure return results, or bypass the re-check of pre-filtered results.

Include what you did, what you expected, what happened, and the version or commit. You will get an
acknowledgement within 3 working days and a status update within 10. Fixes for confirmed issues
are released as soon as they are ready, with credit to the reporter unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| Latest minor release | Yes |
| Older releases | Security fixes are backported to the previous minor release for 6 months after 1.0 |

## Scope

In scope: the `needtoknow` package and its adapters. The example deployment in `examples/gateway`
is a demonstration; its threat model and known limits are in
[examples/gateway/threat-model.md](examples/gateway/threat-model.md). The demo keys and passwords
in that example are public on purpose and are not vulnerabilities.

## Verifying a release

Releases are built and published by [`release.yml`](.github/workflows/release.yml) with PyPI
trusted publishing; no person or long-lived token uploads them. The tools that build them (`build`,
the `hatchling` backend and their dependencies) are pinned by hash in
[`.github/requirements/build.txt`](.github/requirements/build.txt), and the build runs without
fetching anything else. Each file on PyPI carries a PEP 740 attestation that ties it to that
workflow run. From 1.1.0, the GitHub release also carries the same
files, a Sigstore signature for each (`*.sigstore.json`) and a CycloneDX SBOM (`*.cdx.json`).

To check a downloaded file against its signature, with the version you downloaded:

```bash
python -m pip install sigstore
python -m sigstore verify identity \
  --cert-identity "https://github.com/muzakkirhussain011/needtoknow/.github/workflows/release.yml@refs/tags/v1.1.0" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --bundle needtoknow-1.1.0-py3-none-any.whl.sigstore.json \
  needtoknow-1.1.0-py3-none-any.whl
```

A file from PyPI and the same file from the GitHub release are byte-identical: both come from one
build.
