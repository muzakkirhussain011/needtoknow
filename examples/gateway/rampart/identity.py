"""Who is calling: API keys are stored only as HMAC-SHA256 digests and resolve to a Principal."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SENSITIVITY_LEVELS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

# The secret the demo principals file is hashed under. It is published here and its keys in the
# README, so the gateway refuses it unless RAMPART_DEMO=1.
DEMO_KEY_SECRET = b"rampart demo key secret: published, never use it"  # noqa: S105


@dataclass(frozen=True)
class KeyHashing:
    """How API keys are hashed: HMAC-SHA256 under a secret the server holds (RAMPART_KEY_SECRET).

    Keys from `rampart new-key` carry 256 random bits, so no one guesses them, with or without the
    principals file; the secret, which never sits in that file, makes a copy of it useless on its
    own. A keyed hash costs about a microsecond, so callers sending made-up keys cannot tie up the
    gateway, as they could when every request paid for a slow password hash before authentication.
    """

    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("the key secret must be at least 32 random bytes (64 hex characters)")

    @classmethod
    def generate(cls) -> KeyHashing:
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_hex(cls, value: str) -> KeyHashing:
        try:
            secret = bytes.fromhex(value.strip())
        except ValueError:
            raise ValueError("the key secret must be hex, as `rampart key-hashing` prints it") from None
        return cls(secret)

    @property
    def secret_id(self) -> str:
        """Names the secret without revealing it, so a principals file records which secret it needs."""
        return hmac.new(self.secret, b"rampart key secret id", hashlib.sha256).hexdigest()[:16]

    @property
    def is_demo(self) -> bool:
        return hmac.compare_digest(self.secret, DEMO_KEY_SECRET)

    def to_config(self) -> dict[str, Any]:
        return {"scheme": "hmac-sha256", "secret_id": self.secret_id}

    def digest(self, api_key: str) -> str:
        """The hex digest stored for `api_key`, as `key_hmac` in the principals file."""
        return hmac.new(self.secret, api_key.encode("utf-8"), hashlib.sha256).hexdigest()

    def derive(self, purpose: str) -> bytes:
        """A key of its own for `purpose` (such as the audit log's question fingerprints), derived from
        the secret so the gateway holds one secret. What is hashed contains a NUL byte, which no API key
        sent in a header can contain, so a derived key is never the `key_hmac` of a key anyone can use."""
        return hmac.new(self.secret, b"rampart\x00" + purpose.encode("utf-8"), hashlib.sha256).digest()


def key_hashing(secret: str | None, demo: bool = False) -> KeyHashing:
    """The key hashing the gateway runs with: `secret` in hex (RAMPART_KEY_SECRET), or the published
    demo secret when `demo` (RAMPART_DEMO=1) is set and no secret is. The demo secret is refused
    without `demo`."""
    if not secret:
        if demo:
            return KeyHashing(DEMO_KEY_SECRET)
        raise ValueError(
            "set RAMPART_KEY_SECRET to the secret the principals file was hashed under "
            "(`rampart key-hashing` makes one), or RAMPART_DEMO=1 to run the demo"
        )
    hashing = KeyHashing.from_hex(secret)
    if hashing.is_demo and not demo:
        raise ValueError("RAMPART_KEY_SECRET is the published demo secret; it is accepted only with RAMPART_DEMO=1")
    return hashing


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    team: str
    department: str
    clearance: str = "internal"
    roles: tuple[str, ...] = field(default_factory=tuple)
    disabled: bool = False

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_policy_input(self) -> dict:
        data = asdict(self)
        data["roles"] = list(self.roles)
        return data


class PrincipalDirectory:
    """Maps API-key digests to principals. Loaded from YAML so access is reviewable in git."""

    def __init__(self, entries: dict[str, Principal], hashing: KeyHashing):
        self._by_hash = entries
        self.hashing = hashing

    @classmethod
    def from_yaml(cls, path: Path, hashing: KeyHashing) -> PrincipalDirectory:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = raw.get("key_hashing")
        if not isinstance(config, dict) or config.get("scheme") != "hmac-sha256":
            # No fallback to the scrypt or SHA-256 hashes of earlier versions: hash each key again.
            raise ValueError(
                "the principals file needs a key_hashing block with scheme: hmac-sha256; start one with "
                "`rampart key-hashing` and hash each key again with `rampart hash-key`"
            )
        if str(config.get("secret_id")) != hashing.secret_id:
            raise ValueError(
                "the principals file was hashed under another key secret (its key_hashing secret_id differs): "
                "set RAMPART_KEY_SECRET to that secret, or hash each key again"
            )
        entries: dict[str, Principal] = {}
        for item in raw.get("principals", []):
            if "key_hmac" not in item:
                raise ValueError(f"{item.get('user_id')!r} has no key_hmac; hash its key with `rampart hash-key`")
            clearance = item.get("clearance", "internal")
            if clearance not in SENSITIVITY_LEVELS:
                raise ValueError(f"unknown clearance {clearance!r} for {item.get('user_id')}")
            principal = Principal(
                user_id=item["user_id"],
                name=item.get("name", item["user_id"]),
                team=item["team"],
                department=item["department"],
                clearance=clearance,
                roles=tuple(item.get("roles", [])),
                disabled=bool(item.get("disabled", False)),
            )
            entries[str(item["key_hmac"]).lower()] = principal
        return cls(entries, hashing)

    def authenticate(self, api_key: str | None) -> Principal | None:
        if not api_key:
            return None
        candidate = self.hashing.digest(api_key)
        # compare_digest on every entry keeps lookup time independent of where a match is.
        found = None
        for stored_hash, principal in self._by_hash.items():
            if hmac.compare_digest(stored_hash, candidate):
                found = principal
        return found
