### Security

- Example gateway (`examples/gateway`): the audit record links the records of one question by an
  HMAC-SHA256 under a key derived from `RAMPART_KEY_SECRET` (`question_hmac`) instead of an unkeyed
  SHA-256 (`question_sha256`), which anyone who could read the log could reverse when the only
  unknown in a question was a masked phone or ID number, by hashing every candidate. `rampart
  ingest` and `rampart verify-audit` now need `RAMPART_KEY_SECRET` (or `RAMPART_DEMO=1`), as
  `rampart serve` already did. A request from a disabled principal is now audited, without anything
  it carried, and counted as `account_disabled`; it was refused without a record, like a request
  with an unknown key. Every read of the audit log, through `/v1/audit`, `/v1/audit/verify` or the
  dashboard, is audited too.

### Fixed

- Example gateway: `/v1/chat/completions` accepts a message's `content` as OpenAI's list of text
  parts, joined with newlines, as well as a string; it refused the list with 422, so SDKs and chat
  UIs that send it got no answer. The 4,000-character limit applies to the joined text, a message
  holds at most 50 parts, and a part that is not text (an image, audio, a file) is refused with 422
  naming its type.
- Example gateway: `stream: true` on `/v1/chat/completions` answers in OpenAI's server-sent events
  (`chat.completion.chunk` objects, a last chunk with `finish_reason` and the `rampart` block, a
  usage chunk with `stream_options.include_usage`, then `data: [DONE]`). It was ignored and
  answered with one JSON body, which streaming clients could not read. The answer is checked in
  full first and then streamed in pieces, so the policy, masking, audit and metrics are those of
  any other answer, and a denied request is the usual 403.
- Example gateway: denied ingests and audit reads are counted in `rampart_policy_denials_total`, as
  `ingest_not_allowed` and `audit_not_allowed`; only denied questions were, although the reason
  codes existed. The dashboard's denials by reason count every action too.
- Example gateway: Compose passes the gateway's settings through, with the gateway's defaults.
  `RAMPART_LLM_API_KEY`, needed for a model server that wants a key, `RAMPART_MAX_BODY_BYTES`, the
  embedding model and dimension and the tuning settings could not be set. `RAMPART_LOCAL_MODEL`
  makes the `local-llm` profile pull a smaller chat model and serve it under the name the policy
  approves. `RAMPART_DEFAULT_MODEL` is gone: nothing read it, since the policy chooses the model.

### Documentation

- Example gateway: the README says exactly which requests are audited (not `/v1/me`, nor requests
  refused before the policy is asked with 401, 413 or 422), how the policy chooses the model and how
  to change it or serve a smaller one under the approved name, and how to stream. The quickstart
  waits for the models before ingesting (`docker compose wait ollama-pull`), uses
  `docker compose exec -T`, and says the model server needs the models pulled. It gives the status
  and body of an over-limit request (422) and of a disabled principal (403,
  `{"detail": "account disabled"}`), lists the `admin` demo principal, and explains that the express
  delivery question gets no answer because the vendor FAQ is a single chunk, quarantined whole. The
  threat model covers the keyed fingerprint, audited audit reads and disabled principals, and the
  limit on content parts.
