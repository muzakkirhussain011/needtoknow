"""Authorizers for external permission systems. Each lives in its own module.

The HTTP authorizers (`opa.OPAAuthorizer`, `openfga.OpenFGAAuthorizer`,
`spicedb.SpiceDBAuthorizer`, `cerbos.CerbosAuthorizer`, `keto.KetoAuthorizer`) share one client: it
reuses connections (never across `fork()`), bounds each call by `timeout` (except the DNS lookup
of the host) and each answer by `max_response_bytes`, retries transient failures once, never
follows redirects, honours the standard proxy variables, refuses to send an `Authorization` header
unencrypted over the network unless `allow_insecure=True`, and can pause requests to a failing
server with a `CircuitBreaker`.
"""

from wardowl.authorizers._http import CircuitBreaker

__all__ = ["CircuitBreaker"]
