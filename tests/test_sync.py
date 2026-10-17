from __future__ import annotations

from datetime import datetime, timezone

import pytest

from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.sync import Access, with_access
from needtoknow.sync.google_drive import FIELDS, GoogleDriveACL, drive_access

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def user(email, role="reader", **extra):
    return {"type": "user", "role": role, "emailAddress": email, **extra}


def group(email, role="reader", **extra):
    return {"type": "group", "role": role, "emailAddress": email, **extra}


# ---- Access and with_access ---------------------------------------------------------------------


def test_access_writes_both_fields_so_every_earlier_grant_is_replaced():
    assert Access().metadata() == {"allowed_users": [], "allowed_groups": []}
    assert Access(["bob", "alice"], ["hr"], everyone=True).metadata() == {
        "allowed_users": ["alice", "bob"],
        "allowed_groups": ["*", "hr"],
    }
    assert Access(["a"]).metadata(users_field="u", groups_field="g") == {"u": ["a"], "g": []}


def test_with_access_keeps_other_metadata_and_drops_old_grants():
    old = {"title": "Plan", "allowed_users": ["mallory"], "allowed_groups": ["*"]}
    assert with_access(old, Access(groups=["eng"])) == {"title": "Plan", "allowed_users": [], "allowed_groups": ["eng"]}


def test_a_group_named_like_the_everyone_marker_is_refused():
    with pytest.raises(ValueError, match="would read as everyone"):
        Access(groups=["*"]).metadata()
    assert Access(groups=["*"]).metadata(everyone="everyone") == {"allowed_users": [], "allowed_groups": ["*"]}


@pytest.mark.parametrize(
    ("users", "groups", "everyone", "error"),
    [
        ("alice", (), False, TypeError),
        ((), "eng", False, TypeError),
        ([""], (), False, ValueError),
        ((), [None], False, ValueError),
        ((), (), "yes", TypeError),
    ],
)
def test_access_rejects_ambiguous_names(users, groups, everyone, error):
    with pytest.raises(error):
        Access(users, groups, everyone)


# ---- drive_access -------------------------------------------------------------------------------


def test_users_and_groups_by_lowercased_address_for_every_reading_role():
    permissions = [
        user("Alice@Example.com", "owner"),
        user("bob@example.com", "writer"),
        user("carol@example.com", "commenter"),
        user("dave@example.com", "fileOrganizer"),
        group("Eng@Example.com", "organizer"),
        group("hr@example.com"),
    ]
    access = drive_access(permissions, now=NOW)
    assert access.users == {"alice@example.com", "bob@example.com", "carol@example.com", "dave@example.com"}
    assert access.groups == {"eng@example.com", "hr@example.com"}
    assert not access.everyone


def test_discoverable_domain_and_public_grants():
    permissions = [
        {"type": "domain", "role": "reader", "domain": "Example.com", "allowFileDiscovery": True},
        {"type": "anyone", "role": "reader", "allowFileDiscovery": True},
    ]
    assert drive_access(permissions, now=NOW) == Access(groups=["domain:example.com"], everyone=True)
    assert drive_access(permissions[:1], domain_prefix="workspace:", now=NOW).groups == {"workspace:example.com"}


def test_sharing_by_link_alone_grants_nothing_unless_asked():
    permissions = [
        {"type": "domain", "role": "reader", "domain": "example.com", "allowFileDiscovery": False},
        {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
        {"type": "anyone", "role": "reader"},
    ]
    assert drive_access(permissions, now=NOW) == Access()
    assert drive_access(permissions, link_sharing=True, now=NOW) == Access(groups=["domain:example.com"], everyone=True)


@pytest.mark.parametrize(
    "permission",
    [
        user("gone@example.com", deleted=True),
        user("past@example.com", expirationTime="2026-10-17T11:59:59.000Z"),
        user("now@example.com", expirationTime="2026-10-17T12:00:00Z"),
        user("garbled@example.com", expirationTime="next week"),
        user("naive@example.com", expirationTime="2027-01-01T00:00:00"),
        user("odd@example.com", expirationTime=20270101),
        group("folder-only@example.com", view="metadata", inheritedPermissionsDisabled=True),
        user("web@example.com", view="published"),
        user("limited@example.com", inheritedPermissionsDisabled=True, permissionDetails=[{"inherited": True}]),
        user("limited-unknown@example.com", inheritedPermissionsDisabled=True),
        user("someone@example.com", role="publishedReader"),
        user("someone@example.com", role="viewer"),
        {"type": "user", "role": "reader"},
        {"type": "user", "role": "reader", "emailAddress": ""},
        {"type": "domain", "role": "reader", "allowFileDiscovery": True},
        {"type": "application", "role": "reader", "emailAddress": "app@example.com"},
        {"role": "reader", "emailAddress": "untyped@example.com"},
        "not a permission",
    ],
)
def test_grants_that_do_not_open_the_content_or_cannot_be_read_grant_nothing(permission):
    assert drive_access([permission], now=NOW) == Access()


def test_a_grant_that_has_not_expired_counts():
    permission = user("temp@example.com", expirationTime="2026-10-17T12:00:01.000Z")
    assert drive_access([permission], now=NOW).users == {"temp@example.com"}


def test_limited_access_keeps_direct_grants_owners_and_organizers():
    inherited, direct = {"inherited": True}, {"inherited": False}
    permissions = [
        user("direct@example.com", inheritedPermissionsDisabled=True, permissionDetails=[direct]),
        user("both@example.com", inheritedPermissionsDisabled=True, permissionDetails=[inherited, direct]),
        user("owner@example.com", "owner", inheritedPermissionsDisabled=True),
        group("organizers@example.com", "organizer", inheritedPermissionsDisabled=True, permissionDetails=[inherited]),
    ]
    access = drive_access(permissions, now=NOW)
    assert access.users == {"direct@example.com", "both@example.com", "owner@example.com"}
    assert access.groups == {"organizers@example.com"}


# ---- GoogleDriveACL -----------------------------------------------------------------------------


class FakeDrive:
    """The slice of a googleapiclient Drive v3 service that GoogleDriveACL uses."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def permissions(self):
        return self

    def list(self, **params):
        self.calls.append(params)
        return self

    def execute(self):
        return self.pages[len(self.calls) - 1]


def test_reads_every_page_of_a_files_permissions_including_shared_drives():
    drive = FakeDrive(
        [
            {"permissions": [user("alice@example.com")], "nextPageToken": "p2"},
            {"permissions": [group("eng@example.com", "organizer")], "nextPageToken": ""},
        ]
    )
    assert GoogleDriveACL(drive).access("file-1") == Access(["alice@example.com"], ["eng@example.com"])
    base = {"fileId": "file-1", "fields": FIELDS, "supportsAllDrives": True, "pageSize": 100}
    assert drive.calls == [base, {**base, "pageToken": "p2"}]


def test_a_file_with_no_permissions_is_readable_by_nobody():
    assert GoogleDriveACL(FakeDrive([{}])).access("file-1") == Access()


def test_link_sharing_and_domain_prefix_are_passed_through():
    page = {"permissions": [{"type": "domain", "role": "reader", "domain": "example.com"}]}
    acl = GoogleDriveACL(FakeDrive([page]), link_sharing=True, domain_prefix="d/")
    assert acl.access("file-1").groups == {"d/example.com"}


def test_api_errors_are_raised_not_read_as_no_access():
    class Failing(FakeDrive):
        def execute(self):
            raise RuntimeError("403 insufficientFilePermissions")

    with pytest.raises(RuntimeError, match="403"):
        GoogleDriveACL(Failing([])).access("file-1")


def test_permissions_synced_from_drive_decide_what_a_search_returns():
    backend = InMemoryBackend()
    backend.add([Document("plan#0", "launch plan", {"title": "Plan", "allowed_groups": ["*"]})])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    alice = Subject("alice@example.com", groups=["domain:example.com"])
    bob = Subject("bob@example.com", groups=["domain:example.com", "eng@example.com"])
    assert [h.id for h in retriever.retrieve("launch plan", alice).hits] == ["plan#0"]

    drive = GoogleDriveACL(FakeDrive([{"permissions": [user("owner@example.com", "owner"), group("eng@example.com")]}]))
    chunks = {"plan#0": {"title": "Plan", "allowed_groups": ["*"]}}
    access = drive.access("plan")
    backend.set_metadata({cid: with_access(metadata, access) for cid, metadata in chunks.items()})

    assert retriever.retrieve("launch plan", alice).hits == []
    assert [h.metadata["title"] for h in retriever.retrieve("launch plan", bob).hits] == ["Plan"]
