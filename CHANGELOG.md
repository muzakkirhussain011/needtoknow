# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Request pipeline: authenticate, pseudonymise PII, score prompt injection, policy decision,
  permission-aware hybrid retrieval, per-chunk ACL re-check and quarantine, generation, output
  masking, policy-gated PII restore, usage accounting and audit.
- Rego policy for readable collections, model allow-lists, daily token budgets, PII reveal and
  injection blocking, with 14 policy tests.
- PostgreSQL + pgvector store: HNSW and full-text search fused with reciprocal rank fusion, ACL in
  the query, iterative index scans.
- Hash-chained, append-only audit log with verification endpoint and CLI.
- HTTP API including an OpenAI-compatible `/v1/chat/completions`, Prometheus metrics and an
  auditor dashboard.
- Docker image, Compose stacks for running and for integration tests, CI and Dependabot.
- README, threat model and architecture decision records.
