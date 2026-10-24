### Security

- `CedarAuthorizer` no longer leaves out metadata keys that start with `__` or are not strings. A
  chunk holding `{"__restricted": true}` lost the key, so `forbid ... when { resource has
  "__restricted" }` never applied and a broad `permit` let the chunk through. Cedar's JSON reserves
  only `__entity`, `__extn` and `__expr`, for its escapes, so other keys that start with `__` now
  reach Cedar (with `schema`, declare them like any other attribute). A chunk whose metadata holds
  a reserved key or a key that is not a string, at any depth, is denied without asking Cedar and
  counted in the log, like a value Cedar cannot hold; a subject attribute holding one raises
  `AuthorizationError`. The entity references wardowl writes itself, and the `entities` you pass,
  are unchanged.

### Fixed

- `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without sending it, a chunk whose resource id
  the service would refuse: for OpenFGA an id with `:`, `#`, a space or a control character, the id
  `*`, or one that makes the object (`document:<id>`) longer than 256 characters; for SpiceDB
  anything but 1 to 1024 letters, digits and `/_|-=+`. One such id used to make the service refuse
  the whole request, so every retrieval that found that chunk returned nothing, for every user.

### Correction

In the Unreleased `### Security` section, replace the first sentence of the bullet that starts
"The HTTP authorizers' `timeout` now bounds a whole call" with these two:

The HTTP authorizers' `timeout` now bounds a whole call except the DNS lookup of the host:
connecting to each of its addresses, the TLS handshake, sending, every read of the answer, and any
retry. The standard library has no timeout for the lookup, so the system resolver's own settings
bound it, and the time it takes counts against `timeout`.
