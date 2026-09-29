"""
Accounts: sign up with a username and password, up to 10 accounts.
===================================================================
Each account keeps its own:
  * trading settings (leverage, batch, exits...), restored at sign-in;
  * exchange API keys, ENCRYPTED the moment they are saved;
  * paper/live results (the trade log is tagged with the username);
  * whether its paper trader was running, so paper resumes after a restart
    (live never resumes on its own).

Storage: DATA_DIR/accounts.json (default ./logs). On Render the disk is wiped
on every deploy unless a persistent disk is mounted at DATA_DIR.

Security:
  * passwords: salted PBKDF2-SHA256 (200k rounds), never stored in the clear;
  * API keys: Fernet (AES-128-CBC + HMAC-SHA256) under a key derived from
    KEYS_SECRET. Without KEYS_SECRET a random key is created once in
    DATA_DIR/.keys_secret (readable only by the server user); set KEYS_SECRET
    so the key does not sit next to the data it protects;
  * sessions: signed cookies (HMAC-SHA256) keyed by SESSION_SECRET, or a
    random secret kept in DATA_DIR/.session_secret;
  * sign-in throttled: 8 misses per IP in 15 minutes;
  * optional SIGNUP_CODE: when set, creating an account needs it.
"""
import os, json, time, hmac, base64, hashlib, secrets, re, threading
from cryptography.fernet import Fernet, InvalidToken

MAX_USERS = 10
ITER = 200_000
SESSION_HOURS = 24 * 30
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITER)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _check(password, stored):
    try:
        salt_b64, _ = stored.split("$", 1)
        return hmac.compare_digest(_hash(password, base64.b64decode(salt_b64)), stored)
    except Exception:
        return False


def _secret_file(path, nbytes=32):
    """Read a random secret from `path`, creating it (mode 600) the first time."""
    try:
        with open(path, "rb") as f:
            v = f.read()
            if len(v) >= 16:
                return v
    except OSError:
        pass
    v = secrets.token_bytes(nbytes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(v)
    return v


class Accounts:
    def __init__(self, data_dir=None, env=None):
        env = env if env is not None else os.environ
        self.dir = data_dir or env.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        self.path = os.path.join(self.dir, "accounts.json")
        self.signup_code = env.get("SIGNUP_CODE", "")
        ks = env.get("KEYS_SECRET", "")
        self.keys_secret_from_env = bool(ks)
        raw = ks.encode() if ks else _secret_file(os.path.join(self.dir, ".keys_secret"))
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"pltr-keys|" + raw).digest()))
        ss = env.get("SESSION_SECRET", "")
        self.session_key = ss.encode() if ss else _secret_file(os.path.join(self.dir, ".session_secret"))
        self.lock = threading.Lock()
        self.users = self._load()
        self.fails = {}

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return {k: v for k, v in d.items() if NAME_RE.match(k) and v.get("hash")}
        except Exception:
            return {}

    def _save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.users, f)
        os.replace(tmp, self.path)

    def names(self):
        return sorted(self.users)

    # ------------------------------------------------------------ sign up / in
    def register(self, name, password, code=""):
        name = (name or "").strip()
        if self.signup_code and not hmac.compare_digest(code or "", self.signup_code):
            raise PermissionError("sign-up code is wrong")
        if not NAME_RE.match(name):
            raise ValueError("username: 3-32 letters, digits, dot, dash or underscore")
        if len(password or "") < 8:
            raise ValueError("password: at least 8 characters")
        with self.lock:
            if name.lower() in {n.lower() for n in self.users}:
                raise ValueError("that username is taken")
            if len(self.users) >= MAX_USERS:
                raise ValueError(f"the desk is full ({MAX_USERS} accounts)")
            self.users[name] = {"hash": _hash(password), "created": int(time.time()),
                                "settings": {}, "keys": None, "autostart": None}
            self._save()
        return self.token(name)

    def throttled(self, ip):
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < 900]
        return len(self.fails[ip]) >= 8

    def login(self, name, password, ip=""):
        if self.throttled(ip):
            raise PermissionError("too many attempts; try again in 15 minutes")
        name = (name or "").strip()
        u = self.users.get(name)
        if not u or not _check(password or "", u["hash"]):
            self.fails.setdefault(ip, []).append(time.time())
            raise PermissionError("wrong username or password")
        self.fails.pop(ip, None)
        return self.token(name)

    def token(self, name):
        body = base64.urlsafe_b64encode(json.dumps({"u": name, "exp": int(time.time()) + SESSION_HOURS * 3600})
                                        .encode()).decode()
        return body + "." + hmac.new(self.session_key, body.encode(), hashlib.sha256).hexdigest()

    def user(self, token):
        """Username for a session cookie, or None. A deleted account is out at once."""
        try:
            body, sig = (token or "").rsplit(".", 1)
            if not hmac.compare_digest(sig, hmac.new(self.session_key, body.encode(), hashlib.sha256).hexdigest()):
                return None
            d = json.loads(base64.urlsafe_b64decode(body.encode()))
            return d["u"] if d["exp"] >= time.time() and d["u"] in self.users else None
        except Exception:
            return None

    def delete(self, name, password):
        u = self.users.get(name)
        if not u or not _check(password or "", u["hash"]):
            raise PermissionError("wrong password")
        with self.lock:
            del self.users[name]
            self._save()

    # ------------------------------------------------------------ per-account data
    def settings(self, name):
        return dict((self.users.get(name) or {}).get("settings") or {})

    def save_settings(self, name, cfg):
        with self.lock:
            self.users[name]["settings"] = dict(cfg)
            self._save()

    def set_autostart(self, name, mode):
        with self.lock:
            if self.users.get(name, {}).get("autostart") != mode:
                self.users[name]["autostart"] = mode
                self._save()

    def autostart(self, name):
        return (self.users.get(name) or {}).get("autostart")

    def save_keys(self, name, creds):
        """Encrypt and store {exchange, apiKey, secret, password}. Plain text never touches disk."""
        blob = self.fernet.encrypt(json.dumps(creds).encode()).decode()
        with self.lock:
            self.users[name]["keys"] = blob
            self._save()

    def keys(self, name):
        blob = (self.users.get(name) or {}).get("keys")
        if not blob:
            return None
        try:
            return json.loads(self.fernet.decrypt(blob.encode()))
        except InvalidToken:
            return None                    # KEYS_SECRET changed: the old keys cannot be read

    def clear_keys(self, name):
        with self.lock:
            self.users[name]["keys"] = None
            self._save()
