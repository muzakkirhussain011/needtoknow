"""Copying permissions from where documents live onto their chunks.

A source system such as Google Drive knows who may read each document. `Access` holds that answer
in the form `MetadataACLAuthorizer` reads: user ids, group names, and whether everyone may read.
A connector in this package reads it from a source, `with_access` writes it into a chunk's
metadata, and the backend's `set_metadata` stores it without embedding the chunk again::

    access = drive.access(file_id)
    backend.set_metadata({cid: with_access(metadata, access) for cid, metadata in chunks.items()})

Connectors fail closed: a grant they cannot read, or one that does not let its holder read the
document's content, grants nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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

    `Access()` grants nobody: the safe answer when a source says nothing.
    """

    users: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    everyone: bool = False

    def __init__(self, users: Iterable[str] = (), groups: Iterable[str] = (), everyone: bool = False) -> None:
        if not isinstance(everyone, bool):
            raise TypeError("everyone must be True or False")
        object.__setattr__(self, "users", _names("users", users))
        object.__setattr__(self, "groups", _names("groups", groups))
        object.__setattr__(self, "everyone", everyone)

    def metadata(
        self,
        *,
        users_field: str = "allowed_users",
        groups_field: str = "allowed_groups",
        everyone: str = "*",
    ) -> dict[str, list[str]]:
        """The permission fields for `MetadataACLAuthorizer`, with its default field names and marker.

        Both fields are always present, so writing them replaces every earlier grant. A group
        named like the `everyone` marker raises `ValueError`: stored, it would let everyone read.
        """
        if everyone in self.groups:
            raise ValueError(f"a group named {everyone!r} would read as everyone")
        groups = (self.groups | {everyone}) if self.everyone else self.groups
        return {users_field: sorted(self.users), groups_field: sorted(groups)}


def with_access(
    metadata: Mapping[str, Any],
    access: Access,
    *,
    users_field: str = "allowed_users",
    groups_field: str = "allowed_groups",
    everyone: str = "*",
) -> dict[str, Any]:
    """A chunk's metadata with its permission fields replaced by `access`, and every other key kept."""
    return {**metadata, **access.metadata(users_field=users_field, groups_field=groups_field, everyone=everyone)}
