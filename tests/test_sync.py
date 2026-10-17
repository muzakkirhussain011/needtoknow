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
    """The slice of a googleapiclient Drive v3 service that GoogleDriveACL uses.

    Each method's answers are given in order; an exception as an answer is raised by `execute`.
    """

    def __init__(self, pages, *, drive_id=None, restrictions=None):
        self.answers = {
            "permissions.list": iter(pages),
            "files.get": iter([{"driveId": drive_id} if drive_id else {}]),
            "drives.get": iter([{} if restrictions is None else {"restrictions": restrictions}]),
        }
        self.calls = []

    def permissions(self):
        return FakeResource(self, "permissions")

    def files(self):
        return FakeResource(self, "files")

    def drives(self):
        return FakeResource(self, "drives")


class FakeResource:
    def __init__(self, drive, name):
        self.drive, self.name = drive, name

    def list(self, **params):
        return self._call("list", params)

    def get(self, **params):
        return self._call("get", params)

    def _call(self, method, params):
        self.drive.calls.append((f"{self.name}.{method}", params))
        answer = next(self.drive.answers[f"{self.name}.{method}"])
        return FakeRequest(answer)


class FakeRequest:
    def __init__(self, answer):
        self.answer = answer

    def execute(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_reads_every_page_of_a_files_permissions_including_shared_drives():
    drive = FakeDrive(
        [
            {"permissions": [user("alice@example.com")], "nextPageToken": "p2"},
            {"permissions": [group("eng@example.com", "organizer")], "nextPageToken": ""},
        ]
    )
    assert GoogleDriveACL(drive).access("file-1") == Access(["alice@example.com"], ["eng@example.com"])
    base = {"fileId": "file-1", "fields": FIELDS, "supportsAllDrives": True, "pageSize": 100}
    assert drive.calls == [
        ("files.get", {"fileId": "file-1", "fields": "driveId", "supportsAllDrives": True}),
        ("permissions.list", base),
        ("permissions.list", {**base, "pageToken": "p2"}),
    ]


def test_a_file_with_no_permissions_is_readable_by_nobody():
    assert GoogleDriveACL(FakeDrive([{}])).access("file-1") == Access()


def test_link_sharing_and_domain_prefix_are_passed_through():
    page = {"permissions": [{"type": "domain", "role": "reader", "domain": "example.com"}]}
    acl = GoogleDriveACL(FakeDrive([page]), link_sharing=True, domain_prefix="d/")
    assert acl.access("file-1").groups == {"d/example.com"}


def member(email, role="reader"):
    return user(email, role, permissionDetails=[{"permissionType": "member", "role": role, "inherited": True}])


def test_a_shared_drive_open_only_to_members_keeps_only_member_grants():
    page = {
        "permissions": [
            member("alice@example.com"),
            user("guest@partner.com", permissionDetails=[{"permissionType": "file", "inherited": False}]),
            user("unknown@example.com"),
            {"type": "anyone", "role": "reader", "allowFileDiscovery": True},
        ]
    }
    drive = FakeDrive([page], drive_id="drive-1", restrictions={"driveMembersOnly": True})
    assert GoogleDriveACL(drive).access("file-1") == Access(["alice@example.com"])
    assert drive.calls[1] == ("drives.get", {"driveId": "drive-1", "fields": "restrictions"})


def test_an_unrestricted_shared_drive_keeps_every_grant():
    page = {"permissions": [member("alice@example.com"), user("guest@partner.com")]}
    drive = FakeDrive([page], drive_id="drive-1", restrictions={"driveMembersOnly": False})
    assert GoogleDriveACL(drive).access("file-1").users == {"alice@example.com", "guest@partner.com"}


def test_a_shared_drive_open_only_to_its_domain_needs_the_domains_and_keeps_grants_inside_them():
    page = {
        "permissions": [
            member("alice@example.com"),
            user("guest@partner.com"),
            group("eng@EXAMPLE.com"),
            {"type": "domain", "role": "reader", "domain": "partner.com", "allowFileDiscovery": True},
            {"type": "anyone", "role": "reader", "allowFileDiscovery": True},
        ]
    }
    restricted = {"domainUsersOnly": True}
    with pytest.raises(ValueError, match="pass domains="):
        GoogleDriveACL(FakeDrive([page], drive_id="d", restrictions=restricted)).access("file-1")
    acl = GoogleDriveACL(FakeDrive([page], drive_id="d", restrictions=restricted), domains=["Example.com"])
    assert acl.access("file-1") == Access(["alice@example.com"], ["eng@example.com", "domain:example.com"])


def test_domains_always_leave_out_grants_outside_them_and_turn_anyone_into_their_users():
    permissions = [
        user("alice@example.com"),
        user("bob@sub.example.com"),
        user("guest@partner.com"),
        {"type": "domain", "role": "reader", "domain": "example.com", "allowFileDiscovery": True},
        {"type": "anyone", "role": "reader", "allowFileDiscovery": True},
    ]
    access = drive_access(permissions, domains=["example.com", "example.org"], now=NOW)
    assert access == Access(["alice@example.com"], ["domain:example.com", "domain:example.org"])
    assert drive_access(permissions[:2], domains=[], now=NOW) == Access()


def test_domains_must_be_a_collection():
    with pytest.raises(TypeError):
        drive_access([], domains="example.com")
    with pytest.raises(TypeError):
        GoogleDriveACL(FakeDrive([]), domains="example.com")


def test_missing_drive_restrictions_restrict_nothing_and_unreadable_ones_raise():
    page = {"permissions": [user("alice@example.com")]}
    assert GoogleDriveACL(FakeDrive([page], drive_id="d")).access("file-1").users == {"alice@example.com"}
    with pytest.raises(ValueError, match="unreadable restrictions"):
        GoogleDriveACL(FakeDrive([page], drive_id="d", restrictions="members only")).access("file-1")


def test_api_errors_are_raised_not_read_as_no_access():
    with pytest.raises(RuntimeError, match="403"):
        GoogleDriveACL(FakeDrive([RuntimeError("403 insufficientFilePermissions")])).access("file-1")


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
