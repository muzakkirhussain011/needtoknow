### Security

- `CedarAuthorizer` no longer leaves out metadata values Cedar cannot hold. A float, None or an
  integer beyond 64 bits used to vanish from a chunk's attributes, so a `forbid ... when { resource
  has x && ... }` stopped applying and a `permit` could let the chunk through (a `sensitivity` of
  `3.0` from a store that returns every number as a float, say). Integers, and floats without a
  fraction, now become Cedar Longs; a chunk whose metadata holds any other such value, directly or
  in a list or record, is denied without asking Cedar and counted with the evaluation errors in the
  log; a subject attribute of that kind raises `AuthorizationError`.
- `CasbinAuthorizer(group_subjects=True)` sends each group as `group_prefix` + the group
  (`"group:eng"`; the prefix defaults to `"group:"` and cannot be empty). Groups used to be sent as
  the bare subject, so a group named like a user or a role (`"admin"`, `"ceo@example.com"`) got that
  user's or role's access. Policies that grant to groups must now name them with the prefix, and a
  subject id that starts with the prefix raises `AuthorizationError`.
- `GoogleDriveACL` and `drive_access` no longer count a Drive grant that has an expiry time. Such a
  grant was kept without its expiry, so the copy on the chunks outlived the share. Pass
  `expiring_grants=True` to count it until it expires: `Access.expires_at` is then the earliest
  expiry among the grants that count, and `with_access` writes it to the chunks as
  `access_expires_at` (UTC, to the second), so the file can be synced again by then. A later sync
  without one removes the field.
- `GoogleDriveACL.access` returns `Access()`, readable by nobody, for a file that is gone or no
  longer visible to the credentials (HTTP 404, or 403 with the reason `notFound` or
  `insufficientFilePermissions`). It used to raise, so a deleted or unshared file kept its old
  grants for good. Rate limits, server errors and every other error are still raised, keeping the
  grants until a sync succeeds.
- The HTTP authorizers (OPA, OpenFGA, SpiceDB, Cerbos, Keto) never use a kept-alive connection in a
  process other than the one that opened it. A worker forked after a request (gunicorn `--preload`
  with a request at start-up, for example) used to share its parent's socket, and the processes
  could read each other's permission answers.
- `OpenFGAAuthorizer` refuses, with `AuthorizationError`, a subject id or group name that is empty or
  contains `:`, `#`, `*` or whitespace. They are written into OpenFGA's `type:id` strings, where
  `bob#manager` would be a userset and `*` the wildcard. Keto, SpiceDB and Cedar send ids in
  separate fields and are unaffected.
- The HTTP authorizers refuse `ssl_context` with an `http://` url, where it was silently ignored,
  and refuse an `Authorization` header (SpiceDB's `token`, for one) over `http://` unless the url
  names this machine and no proxy applies. Pass `allow_insecure=True` to send it over a network you
  trust. Both raise `ValueError` when the authorizer is created.
- The HTTP authorizers' `timeout` now bounds a whole call: connecting, sending, every read of the
  answer, and any retry. It used to apply to each socket operation, so a server that sent its
  answer slowly could hold a retrieval for much longer. Answers larger than the new
  `max_response_bytes` (10 MiB by default) are refused with `AuthorizationError`.

### Documentation

- The OPA, OpenFGA, SpiceDB and Cerbos pages say what `recheck=False` requires: the filter alone
  decides, and `In` matches a list field by any element (and Cerbos's translated `in` and
  `hasIntersection` match a single value), while `check` denies those shapes. Every chunk must store
  each field in the shape the policy assumes; the default re-check drops the others as violations.
  `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without asking, a chunk whose resource field is
  not one string.
