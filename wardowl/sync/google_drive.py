"""Google Drive: who may read a file, from its Drive permissions.

`drive_access` turns the permission resources that the Drive API v3 returns for a file into an
`Access`. `GoogleDriveACL` fetches them with your Drive service, page by page, for files in My
Drive and in shared drives, and applies the shared drive's restrictions. Install Google's client
yourself (`google-api-python-client` and `google-auth`); this module never imports it.

Drive grants map onto subjects like this:

* a user or group grant gives that email address, lowercased, to `users` or `groups`. Groups can
  contain groups, so a subject's `groups` must list every group it belongs to, directly or not;
* a domain grant gives the group `"domain:<domain>"`, which a subject has when its account is in
  that domain;
* an "anyone" grant gives everyone or, when your domains are given, everyone in them.

A grant gives nothing (it fails closed) when it does not let its holder read the file's content,
or when it cannot be read with certainty:

* sharing by link alone, unless you pass `link_sharing=True`. A link-only grant (domain or anyone
  with `allowFileDiscovery` false) lets in whoever has the link, and a search over every document
  would hand the content to people who never had it;
* a grant to a deleted account, whose address may later belong to someone else;
* a grant with an expiry time, unless you pass `expiring_grants=True`: Drive ends it on its own,
  and the copy on the chunks would outlive it. With `expiring_grants=True` it counts until it
  expires, and `Access.expires_at` is the earliest expiry among the grants that count;
* an expired grant, or one whose expiry time cannot be read;
* a grant that belongs to a view (`metadata`: a limited-access folder's details only; `published`);
* an inherited grant on a limited-access item, where only owners, organizers and direct grants
  open it;
* in a shared drive open only to its members, a grant to anyone who is not a member;
* outside your domains, when they are given, and in a shared drive open only to its domain's users;
* a grant with an unknown type or role, or without the address or domain it needs.

A file that is gone, or that the credentials can no longer see, is readable by nobody.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from wardowl.sync import Access

__all__ = ["FIELDS", "READ_ROLES", "GoogleDriveACL", "drive_access"]

FIELDS = (
    "nextPageToken,permissions(id,type,role,emailAddress,domain,allowFileDiscovery,deleted,"
    "expirationTime,view,permissionDetails,inheritedPermissionsDisabled)"
)
"""The fields `GoogleDriveACL` asks Drive for: everything `drive_access` reads."""

READ_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})
"""Every Drive role that can read a file's content."""

_ALWAYS_OPEN = frozenset({"owner", "organizer"})  # open a limited-access item whatever is inherited
_UNREADABLE = datetime.min.replace(tzinfo=UTC)  # an expiry time that cannot be read: long past
_GONE = frozenset({"notFound", "insufficientFilePermissions"})  # 403 reasons for a file out of sight


def _expiry(value: Any) -> datetime | None:
    """When a grant ends: None when it does not, and long ago when its expiry cannot be read."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _UNREADABLE
        if when.utcoffset() is not None:
            return when
    return _UNREADABLE


def _details(permission: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    details = permission.get("permissionDetails")
    return [d for d in details if isinstance(d, Mapping)] if isinstance(details, list) else []


def _reads(permission: Mapping[str, Any], now: datetime, members_only: bool, expiring: bool) -> bool:
    role = permission.get("role")
    if role not in READ_ROLES or permission.get("view") is not None or permission.get("deleted") is True:
        return False
    expiry = _expiry(permission.get("expirationTime"))
    if expiry is not None and (not expiring or expiry <= now):
        return False
    details = _details(permission)
    if members_only and not any(d.get("permissionType") == "member" for d in details):
        return False
    if permission.get("inheritedPermissionsDisabled") is True:
        return role in _ALWAYS_OPEN or any(d.get("inherited") is False for d in details)
    return True


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def drive_access(
    permissions: Iterable[Mapping[str, Any]],
    *,
    link_sharing: bool = False,
    domain_prefix: str = "domain:",
    members_only: bool = False,
    domains: Iterable[str] | None = None,
    expiring_grants: bool = False,
    now: datetime | None = None,
) -> Access:
    """Who may read a file, from the permission resources `permissions.list` returns for it.

    Ask for the fields in `FIELDS`. `link_sharing=True` counts sharing by link as access for the
    whole domain or for everyone. A domain grant gives the group `domain_prefix` + the domain.
    `members_only=True` counts only grants to a shared drive's members. When `domains` is given,
    only grants inside those domains count, and an "anyone" grant gives each of their domain
    groups instead of everyone. A grant with an expiry time counts only with
    `expiring_grants=True`, until it expires, and the answer's `expires_at` is then the earliest
    expiry among the grants that count. Expiry times are compared with `now`, the current time by
    default.
    """
    if isinstance(domains, str):
        raise TypeError("domains must be a collection of domain names, not a single string")
    now = now or datetime.now(UTC)
    inside = None if domains is None else {d.lower() for d in domains}
    users: set[str] = set()
    groups: set[str] = set()
    everyone = False
    expires_at: datetime | None = None
    for permission in permissions:
        if not isinstance(permission, Mapping) or not _reads(permission, now, members_only, expiring_grants):
            continue
        kind = permission.get("type")
        discoverable = permission.get("allowFileDiscovery") is True or link_sharing
        granted = False
        if kind in ("user", "group"):
            address = _lower(permission.get("emailAddress"))
            if address is not None and (inside is None or address.rpartition("@")[2] in inside):
                (users if kind == "user" else groups).add(address)
                granted = True
        elif kind == "domain":
            domain = _lower(permission.get("domain"))
            if domain is not None and discoverable and (inside is None or domain in inside):
                groups.add(domain_prefix + domain)
                granted = True
        elif kind == "anyone" and discoverable:
            granted = True
            if inside is None:
                everyone = True
            else:
                groups.update(domain_prefix + d for d in inside)
        expiry = _expiry(permission.get("expirationTime"))
        if granted and expiry is not None:
            expires_at = expiry if expires_at is None else min(expires_at, expiry)
    return Access(users, groups, everyone, expires_at)


def _status(error: BaseException) -> int | None:
    """The HTTP status of an error from Google's client (`HttpError.resp.status`), if it has one."""
    status = getattr(getattr(error, "resp", None), "status", None)
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


def _reasons(error: BaseException) -> set[str]:
    """The `reason` of each detail of an error from Google's client."""
    found: list[Any] = [getattr(error, "error_details", None)]
    content = getattr(error, "content", None)
    if isinstance(content, (bytes, str)):
        try:
            body = json.loads(content)
        except ValueError:
            body = None
        inner = body.get("error") if isinstance(body, dict) else None
        if isinstance(inner, dict):
            found += [inner.get("errors"), inner.get("details")]
    return {
        d["reason"]
        for items in found
        if isinstance(items, list)
        for d in items
        if isinstance(d, Mapping) and isinstance(d.get("reason"), str)
    }


def _gone(error: BaseException) -> bool:
    """True when Drive says the file is not there for these credentials: deleted, or out of sight."""
    status = _status(error)
    return status == 404 or (status == 403 and bool(_reasons(error) & _GONE))


class GoogleDriveACL:
    """Reads who may read Drive files, through a Drive v3 service.

    `service` is `googleapiclient.discovery.build("drive", "v3", credentials=...)`. Its
    credentials need the `drive.readonly` scope (shared drives' settings need more than
    `drive.metadata.readonly`) and must see each file's sharing and each shared drive's settings.

    `domains` lists your organization's domains. When given, grants outside them count for nothing.
    A file in a shared drive that is open only to its domain's users needs them: reading its
    access without them raises `ValueError`. `expiring_grants=True` counts grants with an expiry
    time until they expire (see `drive_access`).

    A file that is gone, or that the credentials can no longer see (HTTP 404, or 403 with the
    reason `notFound` or `insufficientFilePermissions`), is readable by nobody: `access` returns
    `Access()`, so deleting a file or unsharing it from the indexer revokes access. Other errors
    from the API, such as rate limits and server errors, are raised: a document whose permissions
    could not be read should keep the permissions it has, not lose or gain any.
    """

    def __init__(
        self,
        service: Any,
        *,
        link_sharing: bool = False,
        domain_prefix: str = "domain:",
        domains: Iterable[str] | None = None,
        expiring_grants: bool = False,
    ) -> None:
        if isinstance(domains, str):
            raise TypeError("domains must be a collection of domain names, not a single string")
        self.service = service
        self.link_sharing = link_sharing
        self.domain_prefix = domain_prefix
        self.domains = None if domains is None else frozenset(domains)
        self.expiring_grants = expiring_grants

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

    def restrictions(self, file_id: str) -> Mapping[str, Any]:
        """The restrictions of the shared drive that holds the file; empty for a file in My Drive."""
        file = self.service.files().get(fileId=file_id, fields="driveId", supportsAllDrives=True).execute()
        drive_id = file.get("driveId")
        if not drive_id:
            return {}
        drive = self.service.drives().get(driveId=drive_id, fields="restrictions").execute()
        restrictions = drive.get("restrictions", {})
        if not isinstance(restrictions, Mapping):
            raise ValueError(f"shared drive {drive_id!r} returned unreadable restrictions: {restrictions!r}")
        return restrictions

    def access(self, file_id: str) -> Access:
        """Who may read the file, within its shared drive's restrictions; nobody when it is gone."""
        domains = self.domains
        try:
            restrictions = self.restrictions(file_id)
            if restrictions.get("domainUsersOnly") is True and domains is None:
                raise ValueError(
                    f"file {file_id!r} is in a shared drive open only to its domain's users: pass domains= "
                    "to GoogleDriveACL so grants outside them can be left out"
                )
            permissions = self.permissions(file_id)
        except Exception as exc:
            if _gone(exc):
                return Access()  # deleted, or no longer visible to these credentials
            raise
        return drive_access(
            permissions,
            link_sharing=self.link_sharing,
            domain_prefix=self.domain_prefix,
            members_only=restrictions.get("driveMembersOnly") is True,
            domains=domains,
            expiring_grants=self.expiring_grants,
        )
