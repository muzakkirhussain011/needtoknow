"""Google Drive: who may read a file, from its Drive permissions.

`drive_access` turns the permission resources that the Drive API v3 returns for a file into an
`Access`. `GoogleDriveACL` fetches them with your Drive service, page by page, for files in My
Drive and in shared drives. Install Google's client yourself (`google-api-python-client` and
`google-auth`); this module never imports it.

Drive grants map onto subjects like this:

* a user or group grant gives that email address, lowercased, to `users` or `groups`. Groups can
  contain groups, so a subject's `groups` must list every group it belongs to, directly or not;
* a domain grant gives the group `"domain:<domain>"`, which a subject has when its account is in
  that domain;
* an "anyone" grant gives everyone.

A grant gives nothing (it fails closed) when it does not let its holder read the file's content,
or when it cannot be read with certainty:

* sharing by link alone, unless you pass `link_sharing=True`. A link-only grant (domain or anyone
  with `allowFileDiscovery` false) lets in whoever has the link, and a search over every document
  would hand the content to people who never had it;
* a grant to a deleted account, whose address may later belong to someone else;
* an expired grant, or one whose expiry time cannot be read;
* a grant that belongs to a view (`metadata`: a limited-access folder's details only; `published`);
* an inherited grant on a limited-access item, where only owners, organizers and direct grants
  open it;
* a grant with an unknown type or role, or without the address or domain it needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from needtoknow.sync import Access

__all__ = ["FIELDS", "READ_ROLES", "GoogleDriveACL", "drive_access"]

FIELDS = (
    "nextPageToken,permissions(id,type,role,emailAddress,domain,allowFileDiscovery,deleted,"
    "expirationTime,view,permissionDetails,inheritedPermissionsDisabled)"
)
"""The fields `GoogleDriveACL` asks Drive for: everything `drive_access` reads."""

READ_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})
"""Every Drive role that can read a file's content."""

_ALWAYS_OPEN = frozenset({"owner", "organizer"})  # open a limited-access item whatever is inherited


def _expired(value: Any, now: datetime) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return when.tzinfo is None or when <= now


def _direct(details: Any) -> bool:
    return isinstance(details, list) and any(isinstance(d, Mapping) and d.get("inherited") is False for d in details)


def _reads(permission: Mapping[str, Any], now: datetime) -> bool:
    role = permission.get("role")
    if role not in READ_ROLES or permission.get("view") is not None or permission.get("deleted") is True:
        return False
    if _expired(permission.get("expirationTime"), now):
        return False
    if permission.get("inheritedPermissionsDisabled") is True:
        return role in _ALWAYS_OPEN or _direct(permission.get("permissionDetails"))
    return True


def _address(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def drive_access(
    permissions: Iterable[Mapping[str, Any]],
    *,
    link_sharing: bool = False,
    domain_prefix: str = "domain:",
    now: datetime | None = None,
) -> Access:
    """Who may read a file, from the permission resources `permissions.list` returns for it.

    Ask for the fields in `FIELDS`. `link_sharing=True` counts sharing by link as access for the
    whole domain or for everyone. A domain grant gives the group `domain_prefix` + the domain.
    Expiry times are compared with `now`, the current time by default.
    """
    now = now or datetime.now(timezone.utc)
    users: set[str] = set()
    groups: set[str] = set()
    everyone = False
    for permission in permissions:
        if not isinstance(permission, Mapping) or not _reads(permission, now):
            continue
        kind = permission.get("type")
        discoverable = permission.get("allowFileDiscovery") is True or link_sharing
        if kind in ("user", "group"):
            address = _address(permission.get("emailAddress"))
            if address is not None:
                (users if kind == "user" else groups).add(address)
        elif kind == "domain":
            domain = _address(permission.get("domain"))
            if domain is not None and discoverable:
                groups.add(domain_prefix + domain)
        elif kind == "anyone" and discoverable:
            everyone = True
    return Access(users, groups, everyone)


class GoogleDriveACL:
    """Reads who may read Drive files, through a Drive v3 service.

    `service` is `googleapiclient.discovery.build("drive", "v3", credentials=...)`. Its
    credentials must be able to see each file's sharing, for example a service account with
    domain-wide delegation acting as a Workspace administrator. Errors from the API, such as a file
    the credentials cannot see, are raised: a document whose permissions could not be read should
    keep the permissions it has, not lose or gain any.
    """

    def __init__(self, service: Any, *, link_sharing: bool = False, domain_prefix: str = "domain:") -> None:
        self.service = service
        self.link_sharing = link_sharing
        self.domain_prefix = domain_prefix

    def permissions(self, file_id: str) -> list[Mapping[str, Any]]:
        """Every permission on the file, across all pages."""
        found: list[Mapping[str, Any]] = []
        params: dict[str, Any] = {"fileId": file_id, "fields": FIELDS, "supportsAllDrives": True, "pageSize": 100}
        while True:
            response = self.service.permissions().list(**params).execute()
            found += response.get("permissions", [])
            params["pageToken"] = response.get("nextPageToken")
            if not params["pageToken"]:
                return found

    def access(self, file_id: str) -> Access:
        """Who may read the file."""
        return drive_access(self.permissions(file_id), link_sharing=self.link_sharing, domain_prefix=self.domain_prefix)
