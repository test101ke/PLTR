"""
Offline checks for accounts, encrypted keys and per-account trading. No network.

    python tests/test_accounts.py
"""
import os, sys, json, tempfile
DATA = tempfile.mkdtemp()
os.environ.update({"DATA_DIR": DATA, "LOG_DIR": DATA, "KEYS_SECRET": "test-secret-one"})
os.environ.pop("SIGNUP_CODE", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts as ac

SECRET = "sEcReT-9f8e7d6c5b4a"


def fresh(**env):
    d = tempfile.mkdtemp()
    return ac.Accounts(data_dir=d, env={"KEYS_SECRET": "k1", **env}), d


def test_signup_login_and_unique_names():
    a, _ = fresh()
    tok = a.register("munene", "password123")
    assert a.user(tok) == "munene"
    assert a.user(a.login("munene", "password123")) == "munene"
    for bad in (("Munene", "password123"), ("x", "password123"), ("bob", "short")):
        try:
            a.register(*bad); assert False, bad
        except ValueError:
            pass
    try:
        a.login("munene", "wrong-pass"); assert False
    except PermissionError:
        pass
    assert a.user(tok + "x") is None and a.user("garbage") is None


def test_ten_accounts_max():
    a, _ = fresh()
    for i in range(10):
        a.register(f"friend{i}", "password123")
    try:
        a.register("friend10", "password123"); assert False, "11th account allowed"
    except ValueError as e:
        assert "full" in str(e)


def test_signup_code_when_set():
    a, _ = fresh(SIGNUP_CODE="letmein")
    try:
        a.register("amy", "password123", "nope"); assert False
    except PermissionError:
        pass
    assert a.user(a.register("amy", "password123", "letmein")) == "amy"


def test_login_throttled_after_repeated_misses():
    a, _ = fresh()
    a.register("zed", "password123")
    for _ in range(8):
        try:
            a.login("zed", "bad", ip="1.2.3.4")
        except PermissionError:
            pass
    try:
        a.login("zed", "password123", ip="1.2.3.4"); assert False, "not throttled"
    except PermissionError as e:
        assert "too many" in str(e)


def test_keys_are_encrypted_on_disk_and_bound_to_the_secret():
    a, d = fresh()
    a.register("kay", "password123")
    a.save_keys("kay", {"exchange": "bybit", "apiKey": "AKIA-PUBLIC-1234", "secret": SECRET, "password": ""})
    raw = open(os.path.join(d, "accounts.json")).read()
    assert SECRET not in raw and "AKIA-PUBLIC-1234" not in raw, "plain-text key on disk"
    assert oct(os.stat(os.path.join(d, "accounts.json")).st_mode & 0o777) == "0o600"
    b = ac.Accounts(data_dir=d, env={"KEYS_SECRET": "k1"})        # restart: same secret reads them
    assert b.keys("kay")["secret"] == SECRET
    c = ac.Accounts(data_dir=d, env={"KEYS_SECRET": "different"})
    assert c.keys("kay") is None, "a different KEYS_SECRET must not decrypt"


def test_settings_and_autostart_survive_restart():
    a, d = fresh()
    a.register("sam", "password123")
    a.save_settings("sam", {"leverage": 7}); a.set_autostart("sam", "paper")
    b = ac.Accounts(data_dir=d, env={"KEYS_SECRET": "k1"})
    assert b.settings("sam") == {"leverage": 7} and b.autostart("sam") == "paper"


def test_unwritable_data_dir_falls_back_instead_of_crashing():
    a = ac.Accounts(data_dir="/proc/definitely-not-writable/data", env={"KEYS_SECRET": "k1"})
    assert a.dir.endswith("logs")
    name = "fallback_" + os.urandom(3).hex()          # the fallback folder persists between runs
    a.register(name, "password123")                   # works, no crash
    a.delete(name, "password123")


# ------------------------------------------------------------------ through the web app
def test_app_flow_isolation_and_encryption():
    from fastapi.testclient import TestClient
    import main, trader as tr
    from test_trader import FakeCcxt
    anon = TestClient(main.app)
    assert anon.get("/", follow_redirects=False).status_code == 200, "the dashboard must be open to everyone"
    assert anon.get("/api/state").status_code == 200
    assert anon.get("/api/trade/status").status_code == 401
    assert anon.get("/api/log").status_code == 401, "the raw log holds every account's trades"
    r = anon.get("/trade", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/trade"
    assert anon.get("/login").status_code == 200 and anon.get("/healthz").status_code == 200
    assert anon.post("/api/login", data="name=x&password=y",
                     headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415

    alice, bob = TestClient(main.app), TestClient(main.app)
    assert alice.post("/api/signup", json={"name": "alice", "password": "password123"}).status_code == 200
    assert bob.post("/api/signup", json={"name": "bob", "password": "password456"}).status_code == 200
    assert alice.get("/").status_code == 200 and alice.get("/trade").status_code == 200

    main.trader_for("alice").exchange_factory = lambda *a, **k: FakeCcxt()
    r = alice.post("/api/trade/keys", json={"exchange": "bybit", "apiKey": "ALICEKEY12345678", "secret": SECRET})
    assert r.status_code == 200 and r.json()["keys"]["validated"]["ok"], r.text
    raw = open(os.path.join(DATA, "accounts.json")).read()
    assert SECRET not in raw and "ALICEKEY12345678" not in raw
    st_a, st_b = alice.get("/api/trade/status").json(), bob.get("/api/trade/status").json()
    assert st_a["keys"]["apiKey"] == "ALIC…5678" and SECRET not in json.dumps(st_a)
    assert st_b["keys"] is None, "bob can see alice's keys"

    assert alice.post("/api/trade/config", json={"leverage": 5}).json()["config"]["leverage"] == 5
    assert bob.get("/api/trade/status").json()["config"]["leverage"] == 10, "settings leaked across accounts"
    main.TRADERS.clear()                                          # simulate a restart
    assert alice.get("/api/trade/status").json()["config"]["leverage"] == 5, "settings not restored"
    assert alice.get("/api/trade/status").json()["keys"]["apiKey"] == "ALIC…5678", "keys not restored"

    alice.post("/api/logout", json={})
    alice.cookies.clear()
    assert alice.get("/api/trade/status").status_code == 401


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
