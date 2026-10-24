### Security

- Haystack: a model calling `WardowlRetriever` through `ComponentTool` can no longer choose the
  subject. `ComponentTool` offered `subject` as a tool parameter, and it passes on every argument a
  model sends, even one it does not offer, converting it with pydantic, which built a `Subject` from
  a JSON object. The `subject` input is now typed `Callable[[], Subject] | Any`: `ComponentTool`
  leaves out an input whose type holds a function, and a value a model sends anyway arrives as
  JSON, which the component refuses with nothing retrieved. In an agent, give the tool the subject
  from the agent's state with `inputs_from_state={"subject": "subject"}`. `BoundWardowlRetriever`
  is unchanged.
- LlamaIndex's `PermissionNodePostprocessor` returns copies of the nodes it keeps, with only the
  metadata keys named in the new `metadata_fields`, none by default, in their relationships too, as
  the retrievers already did. It returned the nodes unchanged, and LlamaIndex's default
  `MetadataMode.LLM` put `allowed_users`, `allowed_groups` and `tenant` into prompts and traces. The
  nodes passed in are not changed, and the authorizer still sees all the metadata.

### Fixed

- `PermissionedRetriever(audit_key=...)` raises `TypeError` for a key that is not `bytes` or
  `bytearray`. A string passed the length check, then made every event fail to build: the error was
  logged, no observer received an event, and retrieval went on unaudited.
- A shared-cache entry whose expiry is an integer too large for a float is a miss. `float()` raised
  `OverflowError`, which escaped the read, so every retrieval for that subject failed.
- The conformance kit requires `asearch` to refuse, with `ValueError`, every filter `search`
  refuses. When `search` refused a filter on an odd field name such as `team.name`, the kit skipped
  `asearch`, so an async path that dropped the filter and returned everything went unseen.

### Changed

- Haystack: `WardowlRetriever`'s `subject` input also accepts a function returning the `Subject`,
  called on every run. Because the input accepts any type, a connection to the component by
  component name alone is ambiguous: name the sockets, as in
  `pipeline.connect("embedder.embedding", "retriever.query_embedding")`.
