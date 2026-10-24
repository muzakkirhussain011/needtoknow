# Example: a self-hosted AI gateway (Rampart)

> This is an example deployment in the [wardowl](../../README.md) repository. It started as
> a standalone project called Rampart and is being ported onto the `wardowl` library. Its
> package, settings (`RAMPART_*`) and metrics (`rampart_*`) keep that name until the port lands.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Policy: OPA](https://img.shields.io/badge/policy-OPA%20%2F%20Rego-7d9199.svg)

A self-hosted AI gateway that lets regulated teams use open-weight LLMs on their own documents,
with policy-as-code access control, PII pseudonymisation, prompt-injection defences, per-team
budgets and a tamper-evident audit trail.

## Why

Most companies already know staff want to ask an LLM about the handbook, the incident runbook or
the payment procedure. What stops them is not the model; it is the questions the risk and
compliance teams ask:

| Question from risk / compliance | What Rampart does |
|---|---|
| Does our data leave the building? | Models run on your own hardware (Ollama, vLLM, llama.cpp). Rampart speaks the OpenAI API to them. |
| Can an engineer read HR files through the chatbot? | Access is decided per request by an OPA policy (department, clearance, collection). The filter runs **inside** the retrieval query, and every returned chunk is checked again before it reaches the model. |
| Will personal data end up in prompts and model logs? | Emails, phone numbers, national IDs, IBANs, card numbers and secrets are replaced with placeholders such as `[EMAIL_1]` before anything reaches the model. Only users with the `pii_reader` role see the real values restored in the answer. |
| A vendor document says "ignore previous instructions". Then what? | Retrieved chunks are scored for injection and quarantined; injected questions are denied by policy. Context is framed as data, never instructions. |
| Who asked what, and can the log be edited afterwards? | Every request, allowed or denied, is appended to a SHA-256 hash chain in PostgreSQL. A trigger rejects `UPDATE` and `DELETE`, and `/v1/audit/verify` finds the first altered record. |
| How do we stop one team burning the GPU budget? | Per-team model allow-lists and daily token budgets, enforced in the same policy. |

## Architecture

```mermaid
flowchart TB
    C["Staff tools<br/>OpenAI SDKs, chat UIs, scripts"] -->|API key| AUTH

    subgraph H["Rampart gateway (FastAPI)"]
        AUTH[Authenticate] --> MASK["Mask PII<br/>score injection"]
        MASK --> DEC{Policy decision}
        DEC -->|allow| RET["Hybrid retrieval<br/>ACL inside the query"]
        RET --> GUARD["Re-check ACL<br/>quarantine injected chunks<br/>mask PII in context"]
        GUARD --> GEN[Generate]
        GEN --> OUT["Mask output<br/>restore PII only if allowed"]
        DEC -->|deny| AUD
        OUT --> AUD[Append audit record]
    end

    DEC <-->|"input / decision"| OPA[("OPA<br/>Rego policy")]
    RET <--> PG[("PostgreSQL<br/>pgvector HNSW + full-text")]
    AUD --> PG
    GEN <-->|OpenAI API| LLM["Open-weight model<br/>Ollama / vLLM"]
```

The pipeline lives in [`rampart/gateway.py`](rampart/gateway.py); each stage is timed and exported as a
Prometheus histogram. Design decisions are recorded as ADRs in [`docs/adr`](../../docs/adr), and the
threats each control addresses are in [`threat-model.md`](threat-model.md).

### Retrieval

Hybrid search in a single SQL statement: an HNSW cosine search over `pgvector` embeddings and a
Postgres full-text search, fused with reciprocal rank fusion (k = 60). The caller's allowed
collections and sensitivity ceiling are in the `WHERE` clause of both branches, so chunks the
caller may not read are never ranked. `hnsw.iterative_scan = relaxed_order` (pgvector 0.8+) keeps
recall up when that filter is selective. See [ADR 0002](../../docs/adr/0002-acl-inside-retrieval.md).

### Policy

All authorisation is in [`policies/rampart.rego`](policies/rampart.rego), with data in
[`policies/data.json`](policies/data.json). Default deny; every deny reason is returned to the
caller and written to the audit log.

```json
"collections": {
  "hr-policies":        { "departments": ["hr"],      "sensitivity": "confidential" },
  "finance-procedures": { "departments": ["finance"], "sensitivity": "restricted" }
},
"teams": {
  "interns": { "models": ["gemma3:1b"], "daily_token_budget": 2000 }
}
```

Changing who may read what is a reviewed pull request to the policy, tested with `opa test`
(coverage is gated at 90% in CI), not a code change.

## Quickstart

Requirements: Docker with Compose. The stack is PostgreSQL with pgvector, OPA, the gateway and a
model server. `RAMPART_DEMO=1` lets the gateway start with the demo users below, whose keys are
published; without it, it refuses them. The API listens on `localhost:8080` only.

**Model server on another machine** (recommended; a GPU host running Ollama or vLLM):

```bash
export RAMPART_DEMO=1
export RAMPART_LLM_BASE_URL=http://gpu-host:11434/v1
export RAMPART_EMBED_BASE_URL=http://gpu-host:11434
docker compose up -d --build
```

**Everything on one machine** (downloads `gemma4:e2b` and `embeddinggemma`, a few GB):

```bash
RAMPART_DEMO=1 docker compose --profile local-llm up -d --build
```

Load the sample documents for *Najm Logistics*, a fictional company:

```bash
docker compose exec gateway rampart ingest samples
```

### Try it as different people

The demo users in [`config/principals.yaml`](config/principals.yaml) have keys `demo-key-<name>`.
Only HMAC-SHA256 digests of the keys are stored, under a secret the gateway holds; the demo's
secret is published, which is why it needs `RAMPART_DEMO=1`. For a real deployment, run
`rampart key-hashing` for a new secret (set it as `RAMPART_KEY_SECRET`) and the `key_hashing`
block of a new principals file, give each user a key from `rampart new-key`, and mount that file
at `/app/config/principals.yaml` in place of the demo's (`docker-compose.yml`). The image holds no
principals file.

| User | Team / department | Clearance | Can read |
|---|---|---|---|
| `bob` | platform / engineering | internal | handbook, vendor FAQ, IT runbooks |
| `alice` | people-ops / hr | confidential, `pii_reader` | handbook, vendor FAQ, HR policies |
| `carol` | treasury / finance | restricted | handbook, vendor FAQ, finance procedures |
| `dave` | security | internal, `auditor` | handbook, vendor FAQ, audit log |
| `intern` | interns / engineering | public | nothing above public; only `gemma3:1b` |

```bash
ask() { curl -s localhost:8080/v1/ask -H "Authorization: Bearer demo-key-$1" \
             -H "Content-Type: application/json" -d "$2"; echo; }

# An answer with numbered citations, from the collections Bob may read
ask bob '{"question": "What counts as a SEV1, and who gets paged?"}'

# Asking for a collection you may not read is a 403 with the reason, not a silent drop
ask bob '{"question": "What are the salary bands?", "collections": ["hr-policies"]}'
# {"error":"denied","reasons":["not authorised for collection hr-policies"],"audit_seq":<n>}

# Alice holds pii_reader: the model only ever sees [EMAIL_1]-style placeholders,
# and the real values are restored in her answer
ask alice '{"question": "Who do I contact to update an employee record?"}'

# The vendor FAQ carries an indirect prompt injection; its chunk is quarantined before the model sees it
ask bob '{"question": "How fast are express deliveries?"}'      # "chunks_quarantined": 1

# Model allow-lists per team
ask intern '{"question": "How many days of annual leave do I get?"}'
# {"error":"denied","reasons":["model gemma4:e2b is not approved for team interns", ...],...}

# The audit chain, for auditors
curl -s localhost:8080/v1/audit/verify -H "Authorization: Bearer demo-key-dave"
# {"ok":true,"checked":<n>,"first_bad_seq":null}
```

For auditors, the gateway also serves an HTML dashboard at `/dashboard` (HTTP Basic, user `dave`,
password `demo-key-dave` in the demo) with usage per team, denial reasons, masked PII by type,
quarantined chunks and the audit-chain status.

### Use it from existing tools

`/v1/chat/completions` is OpenAI-compatible, so existing SDKs and chat UIs only need a new base
URL and a Rampart key:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="demo-key-bob")
reply = client.chat.completions.create(
    model="gemma4:e2b",
    messages=[{"role": "user", "content": "What should I check first during a SEV1?"}],
    extra_body={"collections": ["it-runbooks"]},  # optional: narrow the search
)
print(reply.choices[0].message.content)
print(reply.model_extra["rampart"]["citations"])
```

## API

| Method and path | Who | Purpose |
|---|---|---|
| `POST /v1/ask` | any user | Question over the documents the caller may read; returns answer, citations, usage, masked PII counts, audit sequence and stage timings |
| `POST /v1/chat/completions` | any user | OpenAI-compatible wrapper around the same pipeline (extra `rampart` block in the response) |
| `GET /v1/me` | any user | Who you are, which collections you can read, whether PII is revealed to you |
| `POST /v1/documents` | `admin` | Ingest documents (chunked, embedded, upserted) |
| `GET /v1/audit` | `auditor`, `admin` | Recent audit records |
| `GET /v1/audit/verify` | `auditor`, `admin` | Recompute the hash chain |
| `GET /dashboard` | `auditor`, `admin` | HTML overview (HTTP Basic) |
| `GET /metrics` on port 9464 | scraper, internal network | Prometheus metrics (`rampart_requests_total`, `rampart_policy_denials_total`, `rampart_pii_masked_total`, `rampart_chunks_quarantined_total`, `rampart_acl_violations_total`, `rampart_tokens_total`, `rampart_stage_seconds`), served apart from the API. Denials are counted by a fixed reason code, such as `model_not_approved`. |

CLI: `rampart serve`, `rampart ingest <dir>`, `rampart new-key`, `rampart hash-key <key>`, `rampart key-hashing`,
`rampart verify-audit`.

Questions are at most 4,000 characters; `/v1/chat/completions` takes at most 20 messages of at
most 4,000 characters each. Model and collection names are at most 200 characters, and a request
names at most 50 collections.

## Configuration

Environment variables (or a `.env` file):

| Variable | Default | Notes |
|---|---|---|
| `RAMPART_DATABASE_URL` | unset | PostgreSQL with pgvector. Unset means in-memory stores (development and tests only). |
| `RAMPART_OPA_URL` | `http://localhost:8181` | OPA server loaded with `policies/`. If OPA is unreachable, requests fail closed with 503. |
| `RAMPART_LLM_BASE_URL` | `http://localhost:11434/v1` | Any OpenAI-compatible chat endpoint |
| `RAMPART_LLM_API_KEY` | `not-needed-for-ollama` | Sent as a bearer token to the model server |
| `RAMPART_DEFAULT_MODEL` | `gemma4:e2b` | Used when a request names no model; the policy has its own default |
| `RAMPART_EMBED_BASE_URL` | `http://localhost:11434` | Ollama `/api/embed` |
| `RAMPART_EMBED_MODEL`, `RAMPART_EMBED_DIM` | `embeddinggemma`, `768` | The vector column is created with this dimension |
| `RAMPART_PRINCIPALS_FILE` | `config/principals.yaml` | Users, teams, roles and API key digests |
| `RAMPART_KEY_SECRET` | unset | Hex secret the key digests are made under (`rampart key-hashing`); required unless `RAMPART_DEMO=1` |
| `RAMPART_DEMO` | `false` | Accept the demo principals and their published secret. Never in a real deployment. |
| `RAMPART_METRICS_HOST`, `RAMPART_METRICS_PORT` | `127.0.0.1`, `9464` | Where `rampart serve` serves Prometheus metrics; port `0` turns them off. The image binds them to `0.0.0.0` inside the container, and Compose does not publish the port. |
| `RAMPART_TOP_K`, `RAMPART_MAX_CONTEXT_CHARS` | `5`, `6000` | Retrieval depth and context budget |
| `RAMPART_CHUNK_INJECTION_THRESHOLD` | `0.5` | Chunks scoring at or above this are quarantined |
| `RAMPART_MINUTES_SAVED_PER_ANSWER` | `6.0` | Only used for the dashboard's time-saved estimate |

## Development

```bash
pip install -e ".[dev]"
pytest -q                     # unit tests: in-memory stores, a stand-in policy, a fake model
ruff check . && ruff format --check .
opa test policies -v          # Rego unit tests

# Integration tests against real OPA and PostgreSQL + pgvector (no model needed)
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
```

The tests are written as security properties rather than happy paths, for example:
`test_denied_request_is_audited_and_never_reaches_the_model`,
`test_model_never_sees_raw_pii_even_for_authorised_users`,
`test_store_bug_cannot_leak_restricted_chunks` and
`test_postgres_audit_log_is_append_only_and_verifies`.

```
rampart/
  gateway.py     the request pipeline
  policy.py      OPA client (fails closed)
  documents.py   chunking, in-memory and pgvector hybrid stores
  pii.py         detection, pseudonymisation and restore
  injection.py   prompt-injection scoring
  audit.py       hash-chained audit log (memory and Postgres)
  usage.py       per-team daily token accounting
  models.py      OpenAI-compatible chat and Ollama embeddings clients
  app.py         HTTP API and dashboard
policies/        Rego policy, data and tests
samples/         fictional company documents, including a poisoned vendor FAQ
```

## Limits and roadmap

Honest limits of v0.1:

- PII detection is pattern- and checksum-based. It does not find names or free-text addresses; an
  NER model is the planned next step.
- Injection scoring is heuristic. It catches the common phrasings and exfiltration tricks, and the
  `InjectionScorer` interface is there so a classifier can replace it.
- Authentication is API keys from a file. OIDC/SSO is planned.

Next:

- [ ] Evaluation suite: PII precision and recall on a labelled synthetic set, injection detection
      rates, a permission-leak test across every user and collection, latency per stage on a GPU
- [ ] Streaming responses
- [ ] OIDC authentication and group-to-team mapping
- [ ] Helm chart

## License

[MIT](../../LICENSE)
