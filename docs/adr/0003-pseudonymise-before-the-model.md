# 0003. Pseudonymise PII before the model, restore only by policy

Status: accepted, 2026-10-15

## Context

HR and finance documents contain names, emails, phone numbers, national IDs and bank details.
Even with a self-hosted model, prompts end up in model-server logs, traces and caches. Some users
(an HR partner) legitimately need the real values in the answer; most do not.

## Decision

- Before any model call, the question, the conversation history and every retrieved chunk are
  passed through one `Redactor` per request. Each distinct value gets a stable placeholder such as
  `[EMAIL_1]` or `[NATIONAL_ID_2]`, so the model can still reason about "the same person" across
  passages.
- IBANs (mod 97) and card numbers (Luhn) are validated before masking to keep false positives
  down; ordinary numbers such as amounts and dates are left alone.
- The model is told to keep placeholders verbatim. Its output is masked again (in case it
  produced something new), and the placeholders are mapped back to real values only when the
  policy decision says `reveal_pii`.
- The audit log stores the masked question and per-type counts, never the raw values.

## Consequences

- The model never sees raw identifiers, including for users who are allowed to see them.
- Answers that depend on the literal value (for example "is this IBAN Saudi?") lose precision.
- Pattern-based detection misses names and free-text addresses. The detector list is ordered and
  pluggable; an NER model is the planned addition.
