# Compliance mapping

Which controls of common frameworks needtoknow helps you meet, and what stays with you. needtoknow is
a library, not a service: it is not certified or attested against any framework, and using it does
not make a system compliant. An auditor assesses your whole system; this page shows where
needtoknow is the mechanism and where the evidence comes from.

## What needtoknow provides

- **Access control at retrieval.** Every chunk reaching a reranker, a model or a tool is permitted
  for the subject, decided by your authorizer and checked again after a pre-filtered search
  ([Security model](security.md), S1 and S6).
- **Failing closed.** A permission service that fails, times out or answers badly returns nothing
  (S2).
- **Tenant separation.** A subject of one tenant never receives another tenant's chunk, even with
  shared ids and one cache (S3).
- **Bounded revocation.** Cached decisions last at most `ttl`, and `invalidate()` drops them at once
  ([Revocation and consistency](revocation.md)).
- **An audit trail without content.** A `RetrievalEvent` per call, with ids, counts and outcome,
  never query or chunk text (S10), written by `JsonLinesAudit` or exported with OpenTelemetry
  ([Operations](operations.md)).

## Framework mapping

| Framework | Control | How needtoknow helps | Your part |
|---|---|---|---|
| ISO/IEC 27001:2022 | 5.15 Access control, 8.3 Information access restriction | Enforces your access rules on every retrieval, in every supported store | The access policy, and keeping permissions on chunks current |
| ISO/IEC 27001:2022 | 5.18 Access rights | `invalidate()` and `ttl` bound how long a removed right keeps working | Calling `invalidate()` when rights change, and the review process |
| ISO/IEC 27001:2022 | 8.15 Logging, 8.16 Monitoring activities | Retrieval events and metrics, with alerts in the [operations guide](operations.md) | Storing, protecting and reviewing the logs |
| SOC 2 (Trust Services Criteria) | CC6.1, CC6.3 Logical access | Authorization decided per subject and chunk, failing closed | Authentication, provisioning and access reviews |
| SOC 2 (Trust Services Criteria) | CC7.2 Monitoring | Events, `violations` and `CacheStats` to alert on | Monitoring and incident response ([runbook](runbook.md)) |
| GDPR | Art. 25 Data protection by design, Art. 32 Security of processing | Personal data in documents reaches only those permitted, and audit events hold no content | Lawful basis, records of processing, data subject requests |
| HIPAA Security Rule | 45 CFR 164.312(a)(1) Access control, 164.308(a)(4) Information access management | Limits retrieval of ePHI to permitted users | Risk analysis, workforce policies, encryption and transport security |
| HIPAA Security Rule | 45 CFR 164.312(b) Audit controls | Records each retrieval: who, which chunks, outcome | Retaining and examining the records |
| OWASP Top 10 for LLM Applications 2025 | LLM08 Vector and Embedding Weaknesses | Permission-aware retrieval and tenant partitioning, enforced outside the model, which is the mitigation OWASP recommends | Securing the vector store itself |

The HIPAA rows refer to the Security Rule in force. Proposed amendments have not been finalized.

## Retention and deletion

needtoknow stores no documents and keeps no history of its own:

- **Chunks** live in your vector store. Deleting a document there removes it from retrieval at once.
- **Cached decisions and filters** hold permission answers for a subject, never document content,
  and expire within `ttl` (30 seconds by default), in the process and in a shared Redis cache.
- **Audit events** contain the subject id, chunk ids, counts and outcome, and the query only as an
  HMAC when you supply a key. Where they are kept and for how long is set by your observer and your
  log storage, so your retention policy applies to them.

To erase a person's data, delete their documents from the store and their entries from your audit
log under your retention rules; nothing else needs clearing once `ttl` has passed.

## What needtoknow does not cover

Authentication, encryption at rest and in transit, the security of the stores and permission
services, the content of model output, and the organizational controls every framework requires.
See [Threat model](threat-model.md#out-of-scope).
