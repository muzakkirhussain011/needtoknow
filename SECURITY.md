# Security policy

Rampart is a security control, so reports are very welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security > Report a vulnerability** on this repository.
Do not open a public issue for anything that could let someone read data they should not, bypass
the policy, reach the model with unmasked personal data, or alter the audit trail without detection.

Include what you did, what you expected, what happened, and the version or commit. You will get an
acknowledgement within 3 working days and a status update within 10.

## Supported versions

Until 1.0, fixes go to the latest release only.

## Scope

The controls and their known limits are listed in [docs/threat-model.md](docs/threat-model.md).
Issues already listed there as residual risk are known, but ideas for closing them are welcome as
normal issues.

The demo keys in `config/principals.yaml` and the passwords in the compose files are public on
purpose and are not vulnerabilities. Replace them in any real deployment.
