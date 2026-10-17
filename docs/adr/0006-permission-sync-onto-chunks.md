# 0006. Permission sync copies each source's sharing onto the chunks, failing closed

Status: accepted, 2026-10-17

## Context

Most documents a company indexes live in a system that already decides who may read them: Google
Drive, SharePoint, Confluence. Retrieval has to follow those decisions, and keep following them as
sharing changes. Three ways were considered:

- **Ask the source at query time**, hit by hit. Drive and Graph have no "may this user read these
  files" call for a batch of documents, so this is one request per hit, and it can only post-filter:
  a selective user gets almost nothing back (see the benchmarks).
- **Mirror the source's sharing into a relationship store** (OpenFGA, SpiceDB). It works with the
  existing authorizers, but it still needs the same sync, plus a service to run, and the store
  then answers with lists of documents rather than a filter inside the search.
- **Copy each document's sharing onto its chunks**, in the fields `MetadataACLAuthorizer` reads.
  Every store pre-filters on them, nothing is called at query time, and `set_metadata` already
  updates them without re-embedding.

## Decision

`needtoknow.sync` copies sharing onto the chunks.

- A connector reads one document's sharing and returns `Access(users, groups, everyone)`, a
  source-neutral answer. `with_access` writes it into a chunk's metadata, replacing both
  permission fields and keeping every other key, and the backend's `set_metadata` stores it.
- Group membership is not copied. A grant to a group stays a group name, and the application gives
  each subject every group it belongs to, directly or not, when the user signs in. A user joining
  or leaving a group then needs no sync.
- Connectors fail closed. A grant counts only when it opens the document's content and can be read
  with certainty; everything else (sharing by link alone, deleted accounts, expired or unreadable
  grants, view-only grants, grants a container's settings shut out, unknown kinds) grants nothing.
  An error from the source raises, so a failed read never changes a document's permissions.
- Connectors take the source's client from the application and never import it, so they add no
  dependency. Their tests run on the response shapes the source documents.

Google Drive is the first connector (`needtoknow.sync.google_drive`).

## Consequences

- A sharing change applies after the next sync of that document, not at once. Sources with a
  changes feed (Drive's `changes.list`) keep this to the sync interval.
- Subjects must use the source's identifiers: email addresses for Drive. Mixing identity schemes
  denies, it never over-grants.
- Sources whose rules are unions of grants fit one pair of fields. Confluence intersects them (the
  space's permission and every view restriction on the page and its ancestors), which needs one
  pair of fields per level, combined with `AllOf`; that is decided with the Confluence connector,
  and the default field names must stay unreadable for such chunks so a plain
  `MetadataACLAuthorizer` can never read only one level.
- SharePoint returns site-group grants as numeric ids that Graph does not resolve; its connector
  needs SharePoint's own REST API, and live testing before it ships.
