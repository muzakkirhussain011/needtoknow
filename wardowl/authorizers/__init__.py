"""Authorizers for external permission systems. Each lives in its own module.

The HTTP authorizers (`opa.OPAAuthorizer`, `openfga.OpenFGAAuthorizer`,
`spicedb.SpiceDBAuthorizer`, `cerbos.CerbosAuthorizer`) share one client: it reuses connections,
retries transient failures once, never follows redirects, honours the standard proxy variables, and
can pause requests to a failing server with a `CircuitBreaker`.
"""

from wardowl.authorizers._http import CircuitBreaker

__all__ = ["CircuitBreaker"]
