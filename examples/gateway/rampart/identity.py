"""Who is calling: API keys are stored only as scrypt hashes and resolve to a Principal."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SENSITIVITY_LEVELS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


@dataclass(frozen=True)
class KeyHashing:
    """How API keys are hashed: scrypt, with one random salt per principals file.

    scrypt is slow and needs memory on purpose (16 MiB at the minimum settings accepted here), so
    someone holding a copy of the principals file pays that for every key they guess, where a
    plain hash costs nothing. The gateway pays it once per request, in a worker thread.
    """

    salt: bytes
    n: int = 2**14
    r: int = 8
    p: int = 1

    def __post_init__(self) -> None:
        if len(self.salt) < 16:
            raise ValueError("the key salt must be at least 16 random bytes")
        if self.n < 2**14 or self.n & (self.n - 1) or self.r < 8 or self.p < 1:
            raise ValueError("scrypt needs n a power of two of at least 16384, r at least 8 and p at least 1")

    @classmethod
    def generate(cls) -> KeyHashing:
        return cls(secrets.token_bytes(16))

    @classmethod
    def from_config(cls, data: Any) -> KeyHashing:
        if not isinstance(data, dict) or data.get("scheme") != "scrypt" or "salt" not in data:
            raise ValueError("the principals file needs a key_hashing block with scheme: scrypt and a salt")
        salt = bytes.fromhex(str(data["salt"]))
        return cls(salt, int(data.get("n", 2**14)), int(data.get("r", 8)), int(data.get("p", 1)))

    @classmethod
    def from_file(cls, path: Path) -> KeyHashing:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_config(raw.get("key_hashing"))

    def to_config(self) -> dict[str, Any]:
        return {"scheme": "scrypt", "n": self.n, "r": self.r, "p": self.p, "salt": self.salt.hex()}

    def digest(self, api_key: str) -> str:
        """The hex hash stored for `api_key`, as `key_scrypt` in the principals file."""
        memory = 128 * self.r * (self.n + self.p + 2)  # what scrypt needs, with room to spare
        derived = hashlib.scrypt(
            api_key.encode("utf-8"), salt=self.salt, n=self.n, r=self.r, p=self.p, maxmem=2 * memory, dklen=32
        )
        return derived.hex()


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
    """Maps API-key hashes to principals. Loaded from YAML so access is reviewable in git."""

    def __init__(self, entries: dict[str, Principal], hashing: KeyHashing):
        self._by_hash = entries
        self.hashing = hashing

    @classmethod
    def from_yaml(cls, path: Path) -> PrincipalDirectory:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        hashing = KeyHashing.from_config(raw.get("key_hashing"))
        entries: dict[str, Principal] = {}
        for item in raw.get("principals", []):
            if "key_scrypt" not in item:
                # No fallback to the SHA-256 hashes of earlier versions: hash each key again.
                raise ValueError(f"{item.get('user_id')!r} has no key_scrypt; hash its key with `rampart hash-key`")
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
            entries[str(item["key_scrypt"]).lower()] = principal
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
