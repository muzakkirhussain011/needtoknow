# Google Drive

Copies who may read each Drive file onto the file's chunks, so that
[`MetadataACLAuthorizer`](../concepts.md) filters searches the way Drive's own sharing does. Call
it at ingestion, and again whenever a file's sharing changes. No extra dependency: you pass the
Drive service from Google's client, which you install yourself.

```bash
pip install needtoknow google-api-python-client google-auth
```

```python
from google.oauth2 import service_account
from googleapiclient.discovery import build

from needtoknow.sync import with_access
from needtoknow.sync.google_drive import GoogleDriveACL

credentials = service_account.Credentials.from_service_account_file(
    "sync-account.json",
    scopes=["https://www.googleapis.com/auth/drive.readonly"],
    subject="indexer@example.com",  # domain-wide delegation: the account your ingestion reads files as
)
drive = GoogleDriveACL(build("drive", "v3", credentials=credentials), domains=["example.com"])

access = drive.access(file_id)  # who may read the file
backend.set_metadata({cid: with_access(metadata, access) for cid, metadata in chunks.items()})
```

`chunks` maps the file's chunk ids to the metadata you stored with them. `with_access` replaces
`allowed_users` and `allowed_groups` and keeps every other key, and `set_metadata` stores the
result without embedding the chunks again
([Changing a document's permissions](../operations.md#changing-a-documents-permissions)).

## How Drive sharing maps onto subjects

| Drive grant | Gives | A `Subject` matches when |
|---|---|---|
| A user, any role | `allowed_users`: the address, lowercased | its id is that address, lowercased |
| A group, any role | `allowed_groups`: the address, lowercased | its `groups` list the group |
| A domain, findable in search | `allowed_groups`: `"domain:example.com"` | its `groups` list `"domain:example.com"` |
| Anyone, findable in search | `allowed_groups`: `"*"`, or with `domains` each `"domain:..."` | always, or in those domains |

Every Drive role (reader, commenter, writer, file organizer, organizer and owner) can read the
file's content, so each one counts. Use the same addresses as subject ids, and give each subject
the `domain:` group of its own domain.

Drive groups can contain groups, and a grant to a group reaches the members of the groups inside
it. A subject's `groups` must therefore list every group it belongs to, directly or not. Cloud
Identity's
[`searchTransitiveGroups`](https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveGroups)
returns them in one call (Workspace Enterprise and Cloud Identity Premium); otherwise call the
Admin SDK's `groups.list(userKey=...)` for the user and again for each group found. Resolve them
when the user signs in, as for any group-based permission: the chunks need no update when someone
joins or leaves a group.

## What grants nothing

A grant is skipped when it does not let its holder open the file's content, or when it cannot be
read with certainty. Skipping fails closed: the file is readable by fewer people, never more.

- **Sharing by link alone.** "Anyone with the link" and "anyone in example.com with the link" let
  in whoever holds the link. A search across every document would show the content to people who
  never had the link, so these grants count only with `GoogleDriveACL(service, link_sharing=True)`.
  Grants that are also findable in search (`allowFileDiscovery`) always count.
- **Deleted accounts.** A deleted account's address can later be given to someone else.
- **Expired grants**, and grants whose expiry time cannot be read.
- **Grants that belong to a view.** On a limited-access folder, a `metadata` view grant shows the
  folder's details, not its content; `published` grants are left out as well.
- **Inherited grants on a limited-access item.** Only owners, organizers and people added to the
  item directly can open it.
- **Grants to non-members** in a shared drive open only to its members.
- **Grants outside your domains**, when you pass `domains`; see below.
- **Unknown types and roles**, and grants missing the address or domain they need.

## Shared drives and your domains

A shared drive can be open only to its members, or only to users of its domain. Grants made
before the setting changed can still be listed, but they no longer open the file, so
`GoogleDriveACL` reads each file's shared drive settings (`drives.get`) and applies them: in a
members-only drive, only grants that come from drive membership count.

`domains=["example.com"]` lists your organization's domains. Grants to addresses or domains
outside them then count for nothing, and an "anyone" grant gives each of your domains' groups
instead of everyone. Pass it when sharing outside the organization is turned off, when your
subjects only ever come from your own domains, and always for shared drives open only to their
domain's users: a file in such a drive raises `ValueError` without it. A subdomain counts only
when it is listed.

## Credentials

The reader needs the `drive.readonly` scope: `drives.get`, which reads a shared drive's settings,
accepts no narrower one. Use the identity your ingestion already reads the files with, for
example a service account with
[domain-wide delegation](https://support.google.com/a/answer/162106) acting as an indexing user
who can see each file's sharing and is a member of each shared drive you index. An administrator
does not see other users' files or shared drives they are not a member of, so acting as one is
not enough. An error from the API, such as a file the identity cannot see, is raised: a file
whose sharing could not be read keeps the permissions it had until a later sync succeeds.

## Keeping permissions current

Drive's [changes feed](https://developers.google.com/workspace/drive/api/guides/manage-changes)
reports every file whose content or sharing changed since a saved page token. Read it on a
schedule and sync each changed file you index:

```python
changes = build("drive", "v3", credentials=credentials).changes()
token = load_token() or changes.getStartPageToken(supportsAllDrives=True).execute()["startPageToken"]
fields = "nextPageToken,newStartPageToken,changes(fileId,removed)"
while token:
    request = changes.list(pageToken=token, includeItemsFromAllDrives=True, supportsAllDrives=True, fields=fields)
    page = request.execute()
    for change in page.get("changes", []):
        if change.get("removed"):
            backend.delete(chunk_ids(change["fileId"]))
        elif is_indexed(change["fileId"]):
            access = drive.access(change["fileId"])
            backend.set_metadata({cid: with_access(md, access) for cid, md in chunks(change["fileId"]).items()})
    token = page.get("nextPageToken")
    if "newStartPageToken" in page:
        save_token(page["newStartPageToken"])
```

Sharing a folder changes who may read every file inside it, and the feed lists those files. Save
the token only after the files are synced, so a crash repeats the work rather than skipping it.
Until a change is synced, the chunks keep their old permissions; sync often enough that this
window is acceptable, and see [Operations](../operations.md#changing-a-documents-permissions)
for what changes take effect at once.

## Checking the mapping

The tests run the reader against the response shapes Google documents for `permissions.list`,
`files.get` and `drives.get`, not against a live Workspace domain. Before relying on it, compare
its answer with Drive's sharing dialog for a few files of each kind you index: My Drive, shared
drives, limited-access folders and files shared by link.

`drive_access` is the mapping alone, from the permission resources Drive returns to an `Access`.
Use it to test your own data, or with permissions you already fetched:

```python
from needtoknow.sync.google_drive import drive_access

permissions = [{"type": "group", "role": "reader", "emailAddress": "eng@example.com"}]
assert drive_access(permissions).groups == {"eng@example.com"}
```
