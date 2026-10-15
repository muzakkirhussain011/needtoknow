"""Who is calling: API keys are stored only as SHA-256 hashes and resolve to a Principal."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

SENSITIVITY_LEVELS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


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

    def __init__(self, entries: dict[str, Principal]):
        self._by_hash = entries

    @classmethod
    def from_yaml(cls, path: Path) -> PrincipalDirectory:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries: dict[str, Principal] = {}
        for item in raw.get("principals", []):
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
            entries[item["key_sha256"].lower()] = principal
        return cls(entries)

    def authenticate(self, api_key: str | None) -> Principal | None:
        if not api_key:
            return None
        candidate = hash_key(api_key)
        # compare_digest on every entry keeps lookup time independent of where a match is.
        found = None
        for stored_hash, principal in self._by_hash.items():
            if hmac.compare_digest(stored_hash, candidate):
                found = principal
        return found
