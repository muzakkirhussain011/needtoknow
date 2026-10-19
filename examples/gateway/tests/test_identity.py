import hashlib
from pathlib import Path

import pytest
import yaml

from rampart.cli import main
from rampart.identity import KeyHashing, PrincipalDirectory
from tests.conftest import ROOT

PRINCIPALS = ROOT / "config" / "principals.yaml"


def test_each_demo_key_finds_its_principal_and_nothing_else_does():
    directory = PrincipalDirectory.from_yaml(PRINCIPALS)
    for name in ("alice", "bob", "carol", "dave", "admin", "intern"):
        assert directory.authenticate(f"demo-key-{name}").user_id == name
    for wrong in (None, "", "demo-key-bob ", "DEMO-KEY-BOB", "demo-key-nobody"):
        assert directory.authenticate(wrong) is None


def test_keys_are_stored_as_scrypt_hashes_not_fast_hashes():
    raw = yaml.safe_load(PRINCIPALS.read_text(encoding="utf-8"))
    hashing = KeyHashing.from_config(raw["key_hashing"])
    stored = {p["user_id"]: p["key_scrypt"] for p in raw["principals"]}
    assert stored["bob"] == hashing.digest("demo-key-bob")
    # The SHA-256 that earlier versions stored matches nothing now.
    assert hashlib.sha256(b"demo-key-bob").hexdigest() not in stored.values()


def test_the_same_key_hashes_differently_under_another_salt():
    one, two = KeyHashing.generate(), KeyHashing.generate()
    assert one.digest("demo-key-bob") != two.digest("demo-key-bob")


@pytest.mark.parametrize(
    "options",
    [{"salt": b"short"}, {"n": 2**12}, {"n": 3 * 2**13}, {"r": 4}, {"p": 0}],
)
def test_weak_or_invalid_settings_are_refused(options):
    with pytest.raises(ValueError):
        KeyHashing(**{"salt": b"s" * 16, **options})


def test_a_file_with_sha256_hashes_is_refused(tmp_path: Path):
    old = tmp_path / "principals.yaml"
    old.write_text(
        "key_hashing: {scheme: scrypt, salt: 643fe817fe3d6a294768499076ad48a3}\n"
        "principals:\n"
        "  - {user_id: bob, team: platform, department: engineering, key_sha256: " + "0" * 64 + "}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="key_scrypt"):
        PrincipalDirectory.from_yaml(old)
    old.write_text("principals: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="key_hashing"):
        PrincipalDirectory.from_yaml(old)


def test_the_cli_hashes_with_the_salt_of_the_principals_file(capsys, monkeypatch):
    monkeypatch.setenv("RAMPART_PRINCIPALS_FILE", str(PRINCIPALS))
    from rampart.config import get_settings

    get_settings.cache_clear()
    hashing = KeyHashing.from_file(PRINCIPALS)
    try:
        assert main(["hash-key", "demo-key-bob"]) == 0
        assert capsys.readouterr().out.split() == ["key_scrypt:", hashing.digest("demo-key-bob")]
        assert main(["new-key"]) == 0
        lines = capsys.readouterr().out.splitlines()
        key, stored = lines[0].split()[2], lines[1].split()[1]
        assert len(key) >= 40 and stored == hashing.digest(key)
        assert main(["key-hashing"]) == 0
        fresh = KeyHashing.from_config(yaml.safe_load(capsys.readouterr().out)["key_hashing"])
        assert fresh.salt != hashing.salt
    finally:
        get_settings.cache_clear()
