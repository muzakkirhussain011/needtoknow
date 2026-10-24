# Threat model

Scope: Rampart v0.1 deployed inside a company network, in front of a self-hosted model server, used by
staff through API keys. The model server, PostgreSQL and OPA are internal services that only the
gateway can reach.

## Assets

| Asset | Why it matters |
|---|---|
| Restricted and confidential documents (HR, finance) | Disclosure to the wrong department is a privacy and regulatory incident |
| Personal data inside documents and questions | Must not reach the model, model logs or unauthorised readers |
| The audit trail | Evidence of who asked what; worthless if it can be edited silently |
| GPU capacity and token budget | Shared and finite |
| The policy | Defines all of the above; a wrong change is a silent breach |

## Trust boundaries

```mermaid
flowchart LR
    U[Staff and their tools] -- "untrusted input<br/>API key" --> G[Rampart gateway]
    D[Document owners and vendors] -- "untrusted content<br/>via admin ingest" --> G
    G -- "decision input" --> O[(OPA)]
    G -- "masked text only" --> M[Model server]
    G -- "bound parameters" --> P[(PostgreSQL)]
```

Everything that arrives from users, from documents and from the model is treated as untrusted
text. The model has no tools and no network path through Rampart; its only output is an answer that
is masked again before it is returned.

## Threats, controls and residual risk

| # | Threat | Controls in v0.1 | Verified by | Residual risk |
|---|---|---|---|---|
| T1 | A user reads another department's documents through the assistant | OPA decides readable collections from department and clearance; the filter is in the `WHERE` clause of both retrieval branches; the gateway re-checks every chunk and counts violations (`rampart_acl_violations_total`); asking for a forbidden collection is an explicit deny | `test_engineer_context_contains_no_hr_or_finance_text`, `test_store_bug_cannot_leak_restricted_chunks`, `test_pgvector_hybrid_search_respects_acl`, Rego tests | A document ingested into the wrong collection or with the wrong sensitivity is served as labelled. Ingest is admin-only and audited. |
| T2 | Personal data reaches the model, the model's logs or an unauthorised reader | Questions, history and retrieved chunks are pseudonymised before the model call; the model output is masked again; real values are restored only when the policy grants `reveal_pii`; the audit log stores the masked question | `test_model_never_sees_raw_pii_even_for_authorised_users`, `test_pii_stays_masked_for_users_without_the_role`, PII unit tests | Detection is pattern and checksum based: names and free-text addresses are not detected. Document text is sent unmasked to the embedding model at ingest (self-hosted, but still a copy). The audit record keeps a 16-hex-character unsalted SHA-256 of the raw question, which could be brute-forced when the only unknown is a low-entropy value such as an ID number. |
| T3 | Direct prompt injection in the question (override instructions, exfiltrate secrets, jailbreak) | Heuristic scoring of the question; the policy denies at a score of 0.8 or more; the system prompt pins the model to the numbered context | `test_injection_in_the_question_is_blocked`, `test_attacks_score_high`, Rego tests | Novel phrasings can score low. Impact is bounded: the model only sees passages the caller may already read and has no tools. Earlier turns in `/v1/chat/completions` history are masked but not scored. |
| T4 | Indirect prompt injection planted in a document (for example a vendor FAQ telling the assistant to request passwords) | Every retrieved chunk is scored; chunks at or above 0.5 are quarantined, counted and recorded in the audit log; context is framed as data in the system prompt; markdown-image exfiltration and "send to this address" patterns are signals | `test_poisoned_vendor_chunk_is_quarantined` | Quarantine drops the whole chunk, so legitimate text next to the injection is lost for that answer. Paraphrased instructions can pass. |
| T5 | Audit history is edited or deleted to hide activity | SHA-256 hash chain over canonical JSON; appends are serialised with a transaction-level advisory lock; a trigger rejects `UPDATE` and `DELETE`; `/v1/audit/verify` and `rampart verify-audit` report the first broken record | `test_editing_history_is_detected`, `test_postgres_audit_log_is_append_only_and_verifies` | A database superuser or table owner can `TRUNCATE`, drop the trigger, or rewrite the whole chain consistently. Mitigations: run the gateway as a role that does not own the table, and anchor the head hash outside the database periodically (planned). |
| T6 | A stolen or guessed API key | `rampart new-key` issues random 256-bit keys, which cannot be guessed; they are stored as HMAC-SHA256 digests under a secret the gateway holds outside the principals file (`RAMPART_KEY_SECRET`), so a copy of the file alone is useless, and are compared in constant time across all entries. Checking a key costs microseconds, so requests with made-up keys cost the gateway nothing before authentication. The demo principals, hashed under a published secret, are refused unless `RAMPART_DEMO=1`, and the image carries no principals file. A disabled principal is refused with 403 as soon as its key is checked, before its question is masked or scored, and the policy denies it too | `test_missing_key_is_401`, `test_a_disabled_principal_is_refused_before_any_work`, `test_identity.py`, Rego tests | No key expiry or rotation; no per-key rate limit. Requests from a disabled principal are not written to the audit log, like requests with an unknown key. Someone holding both the file and the secret can test guesses offline, which is hopeless only for random keys, so keys chosen by people must not be used. OIDC is on the roadmap. |
| T7 | One team exhausts GPU capacity or budget, or one request ties up the gateway | Per-team model allow-lists and daily token budgets in the policy; `max_output_tokens`. Questions and chat messages are capped at 4,000 characters, a chat request at 20 messages, model and collection names at 200 characters and a request at 50 collections, all before the PII and injection patterns see them, and those patterns run in linear time. A request body over 1 MiB (`RAMPART_MAX_BODY_BYTES`) is refused with 413, by its `Content-Length` and by the bytes received, before it is parsed or the caller is authenticated. | Rego tests, `test_usage_is_recorded_per_team`, `test_chat_messages_are_bounded`, `test_email_detection_takes_linear_time_on_long_runs`, `test_phone_detection_takes_linear_time_on_long_digit_runs`, `test_the_image_rule_still_matches_and_takes_linear_time`, `test_the_send_data_out_rule_still_matches_and_takes_linear_time`, `test_a_body_over_the_limit_is_refused_before_it_is_parsed_or_authenticated` | The budget is checked before a request, so the last request of the day can overshoot by one answer. No request-rate limiting: put a reverse proxy with one in front. |
| T8 | The policy engine is down or returns nothing | The client fails closed: an unreachable OPA is a 503 and an undefined decision is a deny | `OpaPolicyEngine` in `rampart/policy.py`; no dedicated test yet | Availability depends on OPA; run it as a sidecar. A fail-closed test is tracked. |
| T9 | A policy change opens access by mistake | Policy as code with default deny, `opa check --strict`, `opa fmt`, unit tests and a 90% coverage gate in CI; unknown teams and unknown actions are denied | CI `policy` job | Tests only cover the cases someone wrote. Policy changes should require review from the data owner. |
| T10 | SQL or full-text query injection through the question | All values are bound parameters; the `tsquery` is built only from `[a-z0-9]` keywords joined with `|` | `test_pgvector_hybrid_search_respects_acl` | None known. |
| T11 | Usage data read through the metrics, or a caller inflating them | `rampart serve` serves metrics on a port of their own (9464), which Compose does not publish, and Compose publishes the API on `localhost` only; denials are counted by a fixed reason code, never by text the caller chose, such as a model name | `test_metrics_are_not_served_on_the_api_port`, `test_denial_metrics_use_fixed_reason_codes`, `test_serve_checks_the_credentials_and_serves_metrics_on_their_own_port` | The metrics port has no authentication of its own: keep it on the internal network. |

## Out of scope for v0.1

- Compromise of the host, the database server or the model server itself.
- Network attackers between internal services (run them on a private network; add mTLS if needed).
- Model-quality failures such as wrong but harmless answers. Citations are returned so users can check.
