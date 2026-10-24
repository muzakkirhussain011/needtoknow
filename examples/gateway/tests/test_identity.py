import time
from pathlib import Path

import pytest
import yaml

from rampart.app import load_directory
from rampart.cli import main
from rampart.config import Settings, get_settings
from rampart.identity import DEMO_KEY_SECRET, KeyHashing, PrincipalDirectory, key_hashing
from tests.conftest import ROOT

PRINCIPALS = ROOT / "config" / "principals.yaml"
DEMO = KeyHashing(DEMO_KEY_SECRET)


def test_each_demo_key_finds_its_principal_and_nothing_else_does():
    directory = PrincipalDirectory.from_yaml(PRINCIPALS, DEMO)
    for name in ("alice", "bob", "carol", "dave", "admin", "intern"):
        assert directory.authenticate(f"demo-key-{name}").user_id == name
    for wrong in (None, "", "demo-key-bob ", "DEMO-KEY-BOB", "demo-key-nobody"):
        assert directory.authenticate(wrong) is None


def test_keys_are_stored_as_hmac_digests_under_the_servers_secret():
    raw = yaml.safe_load(PRINCIPALS.read_text(encoding="utf-8"))
    stored = {p["user_id"]: p["key_hmac"] for p in raw["principals"]}
    assert stored["bob"] == DEMO.digest("demo-key-bob")
    assert raw["key_hashing"] == {"scheme": "hmac-sha256", "secret_id": DEMO.secret_id}
    assert DEMO_KEY_SECRET.hex() not in PRINCIPALS.read_text(encoding="utf-8")
    # The same key under another secret matches nothing.
    assert KeyHashing.generate().digest("demo-key-bob") not in stored.values()


def test_checking_a_made_up_key_is_cheap():
    directory = PrincipalDirectory.from_yaml(PRINCIPALS, DEMO)
    start = time.perf_counter()
    for i in range(2000):
        assert directory.authenticate(f"made-up-key-{i}") is None
    # scrypt at the settings used before took tens of milliseconds a key; HMAC takes microseconds.
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize("secret", [b"short", b""])
def test_a_short_secret_is_refused(secret):
    with pytest.raises(ValueError):
        KeyHashing(secret)


def test_the_demo_secret_needs_demo_mode():
    assert key_hashing(None, demo=True) == DEMO
    assert key_hashing(DEMO_KEY_SECRET.hex(), demo=True) == DEMO
    with pytest.raises(ValueError, match="RAMPART_DEMO"):
        key_hashing(DEMO_KEY_SECRET.hex())
    with pytest.raises(ValueError, match="RAMPART_KEY_SECRET"):
        key_hashing(None)
    with pytest.raises(ValueError, match="hex"):
        key_hashing("not hex")
    fresh = KeyHashing.generate()
    assert key_hashing(fresh.secret.hex()) == fresh


def test_the_gateway_refuses_the_demo_principals_unless_asked():
    with pytest.raises(ValueError, match="RAMPART_DEMO"):
        load_directory(Settings(principals_file=PRINCIPALS, key_secret=None, demo=False))
    with pytest.raises(ValueError, match="RAMPART_DEMO"):
        load_directory(Settings(principals_file=PRINCIPALS, key_secret=DEMO_KEY_SECRET.hex(), demo=False))
    # Under another secret, the demo file is refused too: it was hashed under the demo secret.
    other = KeyHashing.generate().secret.hex()
    with pytest.raises(ValueError, match="another key secret"):
        load_directory(Settings(principals_file=PRINCIPALS, key_secret=other, demo=False))
    directory = load_directory(Settings(principals_file=PRINCIPALS, key_secret=None, demo=True))
    assert directory.authenticate("demo-key-bob").user_id == "bob"


def test_a_file_with_older_hashes_is_refused(tmp_path: Path):
    old = tmp_path / "principals.yaml"
    old.write_text(
        "key_hashing: {scheme: scrypt, salt: 643fe817fe3d6a294768499076ad48a3}\n"
        "principals:\n"
        "  - {user_id: bob, team: platform, department: engineering, key_scrypt: " + "0" * 64 + "}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hmac-sha256"):
        PrincipalDirectory.from_yaml(old, DEMO)
    old.write_text(
        f"key_hashing: {{scheme: hmac-sha256, secret_id: {DEMO.secret_id}}}\n"
        "principals:\n"
        "  - {user_id: bob, team: platform, department: engineering, key_scrypt: " + "0" * 64 + "}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="key_hmac"):
        PrincipalDirectory.from_yaml(old, DEMO)
    old.write_text("principals: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="key_hashing"):
        PrincipalDirectory.from_yaml(old, DEMO)


@pytest.fixture
def demo_settings(monkeypatch):
    monkeypatch.setenv("RAMPART_PRINCIPALS_FILE", str(PRINCIPALS))
    monkeypatch.setenv("RAMPART_DEMO", "1")
    monkeypatch.delenv("RAMPART_KEY_SECRET", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_cli_hashes_under_the_gateways_secret(capsys, demo_settings):
    assert main(["hash-key", "demo-key-bob"]) == 0
    assert capsys.readouterr().out.split() == ["key_hmac:", DEMO.digest("demo-key-bob")]
    assert main(["new-key"]) == 0
    lines = capsys.readouterr().out.splitlines()
    key, stored = lines[0].split()[2], lines[1].split()[1]
    assert len(key) >= 40 and stored == DEMO.digest(key)
    assert main(["key-hashing"]) == 0
    out = capsys.readouterr().out
    secret = out.splitlines()[0].split()[0].removeprefix("RAMPART_KEY_SECRET=")
    fresh = KeyHashing.from_hex(secret)
    assert fresh != DEMO and yaml.safe_load(out.split("\n\n", 1)[1]) == {"key_hashing": fresh.to_config()}


def test_the_cli_will_not_hash_without_a_secret(capsys, monkeypatch, demo_settings):
    monkeypatch.setenv("RAMPART_DEMO", "0")
    get_settings.cache_clear()
    assert main(["hash-key", "demo-key-bob"]) == 2
    assert "RAMPART_KEY_SECRET" in capsys.readouterr().err


def test_serve_checks_the_credentials_and_serves_metrics_on_their_own_port(monkeypatch, demo_settings):
    started = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: started.update(app=app, api=(host, port)))
    monkeypatch.setattr(
        "prometheus_client.start_http_server",
        lambda port, addr, registry: started.update(metrics=(addr, port), registry=registry),
    )
    assert main(["serve", "--port", "8081"]) == 0
    app = started["app"]
    assert started["api"] == ("0.0.0.0", 8081) and started["metrics"] == ("127.0.0.1", 9464)  # noqa: S104
    assert started["registry"] is app.state.metrics.registry
    assert "/metrics" not in {getattr(route, "path", None) for route in app.routes}
    started.clear()
    assert main(["serve", "--metrics-host", "10.0.0.5", "--metrics-port", "9100"]) == 0
    assert started["metrics"] == ("10.0.0.5", 9100)
    started.clear()
    assert main(["serve", "--metrics-port", "0"]) == 0
    assert "metrics" not in started and "app" in started


def test_serve_refuses_the_demo_principals_without_demo_mode(capsys, monkeypatch, demo_settings):
    monkeypatch.setenv("RAMPART_DEMO", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("the API must not start"))
    monkeypatch.setattr("prometheus_client.start_http_server", lambda *a, **k: pytest.fail("nor the metrics"))
    assert main(["serve"]) == 2
    assert "RAMPART_DEMO" in capsys.readouterr().err
