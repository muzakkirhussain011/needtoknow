# Incident runbook

What to do when something goes wrong: for each situation, what you see, what it means, and the
steps, containment first. [What to alert on](operations.md#what-to-alert-on) lists the signals, and
[When something fails](production.md#when-something-fails) what each failure looks like.

## Be ready before you need it

- **Keep the audit events** for as long as you may need to look back. Each `RetrievalEvent` names the
  subject and the chunks returned, with the time, never the query or the text, so it is safe to keep
  ([Audit events](operations.md#audit-events)).
- **Log what each subject was built from**: the user, groups and tenant from the verified token, with
  the request. The event has the subject's id only, and an investigation needs the rest.
- **Be able to call `invalidate()` in production**, for example from your admin service or from the
  code that changes permissions. With a shared cache one call reaches every replica; without one,
  every process needs the call.
- **Keep chunk ids traceable** to their source document (for example `"handbook#12"`), so you can find
  every chunk of a document whose sharing was wrong.

## Someone saw a document they should not have

**Contain first.**

1. Remove the access at its source: in the permission service, or on the chunks with
   `set_metadata` ([Changing a document's permissions](operations.md#changing-a-documents-permissions)).
2. Call `authorizer.invalidate(user_id)` on the `CachingAuthorizer`, or `invalidate()` for every
   subject if you cannot tell who else is affected. With a shared cache, no retrieval that starts in
   the calling process afterwards uses the old decision, and other replicas stop within `local_ttl`.
3. Keep the audit events and request logs for the period, before any retention job removes them.

**Find out what happened.** Look up the events whose `subject_id` is that user and whose
`returned_ids` include the document's chunks, and note each event's `started_at`. Then go down
this table:

| Possible cause | How to tell | What to do |
|---|---|---|
| The access was allowed at that moment | The sharing or permission history grants it at `started_at` | Nothing failed. Review how the document came to be shared. |
| The retrieval started before the access was removed | `started_at` is before the change | Expected: a result already computed cannot be recalled ([Where the delay comes from](revocation.md#where-the-delay-comes-from), item 5). |
| It was removed, but within the cache's bound | The change came less than `ttl` before `started_at` and nothing called `invalidate()`; or less than `local_ttl` before it, on another replica | The documented bound. Call `invalidate()` from the code that removes access, or lower `ttl` ([Revocation and consistency](revocation.md)). |
| The permission service was behind | Its consistency mode (OpenFGA's `MINIMIZE_LATENCY`, SpiceDB's `minimizeLatency`) or its bundle or policy reload interval covers the gap | Use the stricter modes for this kind of access ([Strict revocation](revocation.md#strict-revocation)). |
| The chunk's metadata was wrong or not yet updated | The metadata stored on the chunk does not match the source document's sharing | Correct it with `set_metadata`, and run your permission sync on every sharing change, not on a schedule only. |
| The subject had a group, tenant or id it should not have | The claims logged with that request | Build the subject only from verified authentication ([Security model](security.md#the-subject-must-come-from-authentication)). |
| None of these | | Treat it as a possible bug in needtoknow: report it privately as [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md) describes, with the versions, the authorizer, the backend and the event. |

Check your version against the project's
[security advisories](https://github.com/muzakkirhussain011/needtoknow/security/advisories) as well.

**Afterwards.** If the chunks reached a model, its answers in that conversation may contain them;
handle that under your data-incident process. Record the cause and what changed, and if the cache's
bound was the cause, decide `ttl` again as the security decision it is.

## The violations count rose

**What it means.** The store returned chunks outside the filter it was given. The re-check
(`recheck=True`, the default) dropped them, so none reached the caller. With `recheck=False`, from
1.4.0 they are matched against the filter in the process and dropped too; before 1.4.0 they were
passed through, so upgrade if you use it.

1. From the events with `violations` above 0, find which retriever, and so which store and index,
   and since when.
2. Look at what changed then: the store's version, the index mapping or schema, the type of a
   permission field (a number where the filter has strings), your own backend's translation, or a
   needtoknow upgrade.
3. Run `needtoknow.testing.check_backend` against a staging copy of that store at that version
   ([conformance kit](extending.md#test-it-with-the-conformance-kit)). It compares the store's
   answers with the reference semantics on generated documents and filters.
4. Report it as SECURITY.md describes: a store that returns what the filter excludes is a bug in its
   translation, even though the re-check contained it.

## Retrievals fail with AuthorizationError

**What it means.** needtoknow could not get a decision it can trust, so it returned nothing. Users
get errors; nothing is exposed.

1. Find which part is failing. From 1.4.0, `RetrievalEvent.cause` (or the span attribute
   `needtoknow.error.cause`) names the underlying error: `TimeoutError` for a slow permission
   service, `ConnectionRefusedError` or another `OSError` for one that cannot be reached, and none
   for an error status (HTTP 503) or an answer in the wrong shape, which the service's own logs
   explain.
2. Check the circuit breaker: while `CircuitBreaker.is_open`, calls are refused without contacting
   the service, for `reset_after` seconds, then one probe goes through.
3. Restore the permission service. Do not work around it with an authorizer that allows on error, by
   removing the authorizer, or with a fallback that grants access: that turns an outage into an
   exposure.

Subjects whose decisions are still cached keep working until their entries expire. Raising `ttl`
during an outage keeps more of them working, and lengthens how long a revocation takes to apply.

## The shared cache is failing, or someone else may have written to it

**Redis is down or slow.** Every read or write that fails counts as a miss, so answers stay correct
and the permission service takes the load. From 1.4.0, `CachingAuthorizer.stats().shared_failures`
counts them. `invalidate()` raises while Redis is unreachable, because a revocation cannot be
confirmed: retry it once Redis is back.

**Someone else may have written to it.**

- With `signing_key` set, planted or altered entries fail their HMAC and are ignored.
- Without it, set a `signing_key` (at least 16 random bytes) on every replica: unsigned entries are
  then ignored. Then restrict who can write to that Redis.
- If the signing key itself may have leaked, replace it with a new random key on every replica, then
  call `invalidate()`. Entries signed with the old key are ignored; while replicas run different
  keys they simply miss each other's entries.

## An advisory affects your version

1. Read the advisory for the affected and patched versions, and any workaround.
2. Upgrade to a patched release of the line you run (supported lines are in
   [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md#supported-versions)),
   for example `pip install --upgrade "needtoknow>=1.2.1,<1.3"`.
3. Read that release's notes in the [changelog](changelog.md) before a rolling upgrade: a release
   that changes shared-cache keys asks you to call `invalidate()` once every replica runs it.
4. Apply the advisory's workaround to anything you cannot upgrade at once.
