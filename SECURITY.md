# Security policy

wardowl decides what data people may see, so security reports are taken seriously and are very
welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security > Report a vulnerability** on this repository.
Do not open a public issue for anything that could let a subject receive a chunk they may not
read, make an authorizer failure return results, or bypass the re-check of pre-filtered results.

Include what you did, what you expected, what happened, and the version or commit. You will get an
acknowledgement within 3 working days and a status update within 10. Fixes for confirmed issues
are released as soon as they are ready, with credit to the reporter unless you prefer otherwise.

## Supported versions

The latest minor release gets every fix. The minor release before it gets security fixes, as patch
releases, for six months after its successor came out. Older releases get none.

The project was called needtoknow until 2.0.0. needtoknow gets no further releases, security fixes
included: the fixes its supported lines would have received are in wardowl 2.0.0, and moving to it
is described in [Upgrading](https://muzakkirhussain011.github.io/wardowl/upgrading.html). The
dates published for needtoknow 1.3 to 1.6 no longer apply.

| Version | Security fixes | Until |
|---|---|---|
| 2.0.x | Yes (latest) | |
| needtoknow 1.6.x and earlier | No: upgrade to wardowl 2.0 | |

## How reports are handled

1. **Acknowledge** within 3 working days, and confirm or rule out the problem, with a status
   update, within 10.
2. **Assess severity** with CVSS 4.0 in the GitHub advisory, read against the
   [threat model](https://muzakkirhussain011.github.io/wardowl/threat-model.html): anything that
   lets a subject receive a chunk they may not read, crosses tenants, or makes a failure return
   results is high or critical.
3. **Fix** in a private fork of the advisory, with a regression test, and release patch versions
   for every supported line at the same time.
4. **Publish** the GitHub Security Advisory, with a CVE assigned through GitHub, when the fixed
   versions are on PyPI. The advisory, the release notes and the changelog's *Security* section say
   what was affected, which versions, how to upgrade, and any workaround. Dependabot alerts reach
   projects that depend on wardowl from the advisory. The aim is to publish within 90 days of the
   report, sooner for anything actively exploited.

Problems found by the maintainers themselves go through the same steps.

## If a release is compromised

Releases come only from `release.yml` in GitHub Actions, through PyPI trusted publishing, so no
upload token exists to steal. If a published file is found altered, or a release turns out to have
been built from code that should not have been in it, the maintainer:

1. yanks the version on PyPI, so new installs stop resolving to it (installs pinned to it keep
   working, which is what yanking is for);
2. publishes an advisory naming the version, the affected files and their SHA-256 hashes;
3. removes the trusted publisher on PyPI until the cause is understood and the workflow or the
   repository is secured again;
4. releases a fixed version from a clean build, and explains in its notes what happened.

Users can tell genuine files from altered ones with the signatures, attestations and provenance
below.

## Scope

In scope: the `wardowl` package and its adapters. The example deployment in `examples/gateway`
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
  --cert-identity "https://github.com/muzakkirhussain011/wardowl/.github/workflows/release.yml@refs/tags/v1.1.0" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --bundle wardowl-1.1.0-py3-none-any.whl.sigstore.json \
  wardowl-1.1.0-py3-none-any.whl
```

A file from PyPI and the same file from the GitHub release are byte-identical: both come from one
build. After each release, `verify-release.yml` installs it from PyPI pinned to the hash of the
GitHub release's wheel, runs the quickstart, and, from 1.4.0, runs the security test suites of that
version against the installed package. Those tests come from the release's source distribution, after
its provenance and its PyPI hash are checked, never from a git checkout of the tag.

From 1.4.0, each release also carries SLSA build provenance (`wardowl-<version>.intoto.jsonl`),
also stored as a GitHub attestation. With the GitHub CLI:

```bash
gh attestation verify wardowl-2.0.0-py3-none-any.whl --repo muzakkirhussain011/wardowl
```
