"""Copying permissions from where documents live onto their chunks.

A source system such as Google Drive knows who may read each document. `Access` holds that answer
in the form `MetadataACLAuthorizer` reads: user ids, group names, and whether everyone may read.
A connector in this package reads it from a source, `with_access` writes it into a chunk's
metadata, and the backend's `set_metadata` stores it without embedding the chunk again::

    access = drive.access(file_id)
    backend.set_metadata({cid: with_access(metadata, access) for cid, metadata in chunks.items()})

Connectors fail closed: a grant they cannot read, or one that does not let its holder read the
document's content, grants nothing. When an answer holds grants that end on their own,
`Access.expires_at` is the earliest of those ends, and `with_access` writes it to the chunks:
sync the document again by then, since `MetadataACLAuthorizer` does not read it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = ["Access", "with_access"]


def _names(kind: str, names: Iterable[str]) -> frozenset[str]:
    if isinstance(names, str):
        # frozenset("eng") would be {"e", "n", "g"}: reject instead of guessing.
        raise TypeError(f"{kind} must be a collection of names, not a single string")
    items = frozenset(names)
    if not all(isinstance(n, str) and n for n in items):
        raise ValueError(f"{kind} must be non-empty strings")
    return items


@dataclass(frozen=True)
class Access:
    """Who may read a document: these users, the members of these groups, or everyone.

    `Access()` grants nobody: the safe answer when a source says nothing. `expires_at`, a
    timezone-aware time, is when the earliest grant in the answer ends: from then on the answer
    may grant more than the source does, so the document must be synced again before it.
    """

    users: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    everyone: bool = False
    expires_at: datetime | None = None

    def __init__(
        self,
        users: Iterable[str] = (),
        groups: Iterable[str] = (),
        everyone: bool = False,
        expires_at: datetime | None = None,
    ) -> None:
        if not isinstance(everyone, bool):
            raise TypeError("everyone must be True or False")
        if expires_at is not None and (not isinstance(expires_at, datetime) or expires_at.utcoffset() is None):
            raise TypeError("expires_at must be a timezone-aware datetime, or None")
        object.__setattr__(self, "users", _names("users", users))
        object.__setattr__(self, "groups", _names("groups", groups))
        object.__setattr__(self, "everyone", everyone)
        object.__setattr__(self, "expires_at", expires_at)

    def metadata(
        self,
        *,
        users_field: str = "allowed_users",
        groups_field: str = "allowed_groups",
        everyone: str = "*",
        expires_field: str = "access_expires_at",
    ) -> dict[str, Any]:
        """The permission fields for `MetadataACLAuthorizer`, with its default field names and marker.

        Both fields are always present, so writing them replaces every earlier grant. A group
        named like the `everyone` marker raises `ValueError`: stored, it would let everyone read.
        When `expires_at` is set, `expires_field` holds it too, in UTC to the second, rounded down
        (`"2026-10-17T12:00:00Z"`), so stored expiries sort as text.
        """
        if everyone in self.groups:
            raise ValueError(f"a group named {everyone!r} would read as everyone")
        groups = (self.groups | {everyone}) if self.everyone else self.groups
        fields: dict[str, Any] = {users_field: sorted(self.users), groups_field: sorted(groups)}
        if self.expires_at is not None:
            fields[expires_field] = self.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return fields


def with_access(
    metadata: Mapping[str, Any],
    access: Access,
    *,
    users_field: str = "allowed_users",
    groups_field: str = "allowed_groups",
    everyone: str = "*",
    expires_field: str = "access_expires_at",
) -> dict[str, Any]:
    """A chunk's metadata with its permission fields replaced by `access`, and every other key kept.

    An expiry written by an earlier sync is removed when `access` has none.
    """
    kept = {key: value for key, value in metadata.items() if key != expires_field}
    fields = access.metadata(
        users_field=users_field, groups_field=groups_field, everyone=everyone, expires_field=expires_field
    )
    return {**kept, **fields}
