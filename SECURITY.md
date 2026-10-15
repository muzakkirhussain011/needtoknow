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
