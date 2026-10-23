# Open Policy Agent

No extra dependency: requests use the standard library.

```python
from wardowl.authorizers.opa import OPAAuthorizer

authorizer = OPAAuthorizer("http://localhost:8181", filter_path="wardowl/filter")
```

Your Rego policy decides; wardowl asks. A complete, tested policy is in
[`examples/opa/wardowl.rego`](https://github.com/muzakkirhussain011/wardowl/blob/main/examples/opa/wardowl.rego):

```bash
opa run --server examples/opa
```

## Decisions

`check` queries `decisions_path` (default `wardowl/decisions`) with this input:

```json
{
  "subject": {"id": "bob", "groups": ["engineering"], "attributes": {}},
  "action": "read",
  "resources": [{"id": "handbook-1", "metadata": {"allowed_groups": ["*"]}}]
}
```

and expects a list of booleans, one per resource, in order. In the example policy:

```text
decisions := [allowed(r) | some r in input.resources]
```

## Filters for pre-filtering

If you set `filter_path`, `filter_for` queries it with `{"subject": ..., "action": ...}` and expects
a filter in JSON form, or `null` when the policy cannot express the subject's access as a filter:

```text
filter := {"or": [
	{"in": {"field": "allowed_users", "values": [input.subject.id]}},
	{"in": {"field": "allowed_groups", "values": array.concat(input.subject.groups, ["*"])}},
]}
```

The JSON form is `{"in": {"field": ..., "values": [...]}}`, `{"and": [...]}` or `{"or": [...]}`,
the same as `filter_to_dict`. The filter must select exactly what `decisions` allows, since the
vector store applies the filter and `decisions` re-checks the results. If the two disagree, the
re-check removes the extra hits and counts them as `violations`.

With `PermissionedRetriever(recheck=False)` there is no re-check: a pre-filtered hit is only
matched against the filter, so the filter alone decides. `In` matches a list field when any of its
elements is among the values, and a single value when it is, even where your Rego treats a field of
that shape differently. Every chunk must then store each field in the shape the policy assumes (a
list for `allowed_groups`, one string for a document id); keep `recheck=True`, the default, unless
that holds for every chunk.

## Failing closed

Every failure raises `AuthorizationError`: an unreachable server, a timeout, a non-200 response,
invalid JSON, an answer larger than `max_response_bytes=` (10 MiB by default), an undefined
decision, a list of the wrong length or type, or a malformed filter. An undefined decision is
treated as a deny, never an allow. `timeout=` (2 seconds by default) bounds the whole call:
connecting, sending, reading every byte of the answer, and any retry.

`headers=` adds headers to every request, for example `{"Authorization": "Bearer ..."}` when OPA
requires a token. An `Authorization` header is refused with `ValueError` over `http://` unless the
url names this machine (`localhost`, `127.0.0.1`, `::1`) and no proxy applies: use `https://`, or
pass `allow_insecure=True` on a network you trust. Connections are reused and transient failures
retried once (`retries=`); a `circuit_breaker=` pauses requests to a failing server, and
`ssl_context=` sets your own CA or mutual TLS for an `https://` url (it is refused with `http://`,
where it would not be used). A connection is never shared across `fork()`: a worker forked after a
request opens its own. See [resilience](../operations.md#resilience-of-remote-permission-services).
