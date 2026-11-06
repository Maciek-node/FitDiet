#!/usr/bin/env python3
"""Small self-hosted HTTP server and account API for Dobry Talerz."""
from __future__ import annotations

import hashlib
import hmac
import argparse
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FITDIET_DATA_DIR", ROOT / ".fitdiet-data")).resolve()
DB_PATH = Path(os.environ.get("FITDIET_DB_PATH", DATA_DIR / "fitdiet.sqlite3")).resolve()
PBKDF2_ROUNDS = 310_000
SESSION_SECONDS = 14 * 24 * 60 * 60
COOKIE_NAME = "fitdiet_session"
MAX_BODY = 6 * 1024 * 1024
STATE_KEYS = {
    "recipes", "plans", "pantry", "checked", "hiddenDietMeals",
    "dietWeekAssignments", "householdSize",
}
FAILED_LOGINS: dict[str, list[float]] = {}
DUMMY_SALT = b"dobry-talerz-login-check"
DUMMY_HASH = hashlib.pbkdf2_hmac("sha256", b"not-a-real-password", DUMMY_SALT, PBKDF2_ROUNDS)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database():
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        DB_PATH.parent.chmod(0o700)
    except OSError:
        pass
    with database() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash BLOB PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                state_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, state_key)
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        """)
    for path in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if path.exists():
            try:
                path.chmod(0o600)
            except OSError:
                pass


def configure_single_user() -> None:
    """Provision the configured owner account and optionally migrate the sole existing account."""
    email = os.environ.get("FITDIET_SINGLE_USER_EMAIL", "").strip().casefold()
    password = os.environ.get("FITDIET_SINGLE_USER_PASSWORD", "")
    if not email and not password:
        return
    if (not email or len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@")
            or len(password) < 10 or len(password) > 128):
        raise RuntimeError("Skonfiguruj poprawne FITDIET_SINGLE_USER_EMAIL oraz hasło o długości 10–128 znaków.")
    with database() as db:
        target = db.execute("SELECT id,password_salt,password_hash FROM users WHERE email=?", (email,)).fetchone()
        if target:
            if not hmac.compare_digest(password_digest(password, target["password_salt"]), target["password_hash"]):
                salt = secrets.token_bytes(16)
                db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",
                           (salt, password_digest(password, salt), target["id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
            return
        existing = db.execute("SELECT id FROM users").fetchall()
        salt = secrets.token_bytes(16)
        digest = password_digest(password, salt)
        if len(existing) == 1:
            # Preserve the current account's recipes and plans when adopting the configured login.
            user_id = existing[0]["id"]
            db.execute("UPDATE users SET email=?,password_salt=?,password_hash=? WHERE id=?",
                       (email, salt, digest, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        else:
            cursor = db.execute("INSERT INTO users(email,password_salt,password_hash,created_at) VALUES(?,?,?,?)",
                                (email, salt, digest, int(time.time())))
            user_id = cursor.lastrowid
        if not db.execute("SELECT 1 FROM user_state WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
            defaults = {"recipes": [], "plans": [], "pantry": [], "checked": [], "hiddenDietMeals": [],
                        "dietWeekAssignments": {}, "householdSize": "2"}
            db.executemany("INSERT INTO user_state(user_id,state_key,value_json,updated_at) VALUES(?,?,?,?)",
                           [(user_id, key, json.dumps(value, ensure_ascii=False), int(time.time()))
                            for key, value in defaults.items()])


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


class Handler(SimpleHTTPRequestHandler):
    server_version = "DobryTalerz/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        decoded = unquote(urlsplit(path).path).lstrip("/")
        parts = Path(decoded).parts
        if any(part.startswith(".") for part in parts) or decoded == "server.py":
            return str(ROOT / "__not_found__")
        return super().translate_path(path)

    def send_json(self, status: int, data: dict, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if size <= 0 or size > MAX_BODY:
            return None
        try:
            payload = json.loads(self.rfile.read(size))
            return payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def origin_is_same(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return bool(origin and urlsplit(origin).netloc.lower() == host.lower())

    def cookie_secure(self) -> bool:
        return os.environ.get("FITDIET_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}

    def cookie_header(self, token: str, max_age: int = SESSION_SECONDS) -> tuple[str, str]:
        secure = "; Secure" if self.cookie_secure() else ""
        return ("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")

    def clear_cookie_header(self) -> tuple[str, str]:
        secure = "; Secure" if self.cookie_secure() else ""
        return ("Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}")

    def session_user(self) -> sqlite3.Row | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[COOKIE_NAME].value
        except Exception:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", "ignore")).digest()
        now = int(time.time())
        with database() as db:
            row = db.execute("""
                SELECT users.id, users.email FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """, (token_hash, now)).fetchone()
            if not row:
                db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        return row

    def new_session(self, user_id: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        with database() as db:
            db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                       (hashlib.sha256(token.encode("ascii")).digest(), user_id, int(time.time()) + SESSION_SECONDS))
        return self.cookie_header(token)

    def require_write_origin(self) -> bool:
        if self.origin_is_same():
            return True
        self.send_json(403, {"error": "Nieprawidłowe źródło żądania."})
        return False

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/auth":
            user = self.session_user()
            self.send_json(200, {"authenticated": bool(user), "user": {"email": user["email"]} if user else None})
        elif route == "/api/state":
            user = self.session_user()
            if not user:
                self.send_json(401, {"error": "Zaloguj się."})
                return
            with database() as db:
                rows = db.execute("SELECT state_key,value_json FROM user_state WHERE user_id=?", (user["id"],)).fetchall()
            self.send_json(200, {"state": {row["state_key"]: json.loads(row["value_json"]) for row in rows}})
        elif route.startswith("/api/"):
            self.send_json(404, {"error": "Nie znaleziono endpointu."})
        else:
            super().do_GET()

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route not in {"/api/signup", "/api/login", "/api/logout"}:
            self.send_json(404, {"error": "Nie znaleziono endpointu."})
            return
        if not self.require_write_origin():
            return
        payload = self.read_json()
        if payload is None:
            self.send_json(400, {"error": "Nieprawidłowe lub zbyt duże żądanie."})
            return
        if route == "/api/signup":
            self.send_json(403, {"error": "Rejestracja jest wyłączona. Skontaktuj się z właścicielem aplikacji."})
        elif route == "/api/login":
            self.login(payload)
        else:
            user = self.session_user()
            if user:
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie[COOKIE_NAME].value
                with database() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode("ascii", "ignore")).digest(),))
            self.send_json(200, {"ok": True}, (self.clear_cookie_header(),))

    def signup(self, payload: dict) -> None:
        email = str(payload.get("email", "")).strip().casefold()
        password = payload.get("password")
        state = payload.get("state")
        if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
            self.send_json(400, {"error": "Podaj poprawny adres e-mail."})
            return
        if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
            self.send_json(400, {"error": "Hasło musi mieć od 10 do 128 znaków."})
            return
        if not isinstance(state, dict) or not set(state).issubset(STATE_KEYS):
            self.send_json(400, {"error": "Nie udało się przygotować danych konta."})
            return
        serialized = {}
        try:
            for key, value in state.items():
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > MAX_BODY:
                    raise ValueError
                serialized[key] = encoded
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Dane konta są nieprawidłowe lub zbyt duże."})
            return
        salt = secrets.token_bytes(16)
        digest = password_digest(password, salt)
        try:
            with database() as db:
                cursor = db.execute("INSERT INTO users(email,password_salt,password_hash,created_at) VALUES(?,?,?,?)",
                                    (email, salt, digest, int(time.time())))
                user_id = cursor.lastrowid
                db.executemany("INSERT INTO user_state(user_id,state_key,value_json,updated_at) VALUES(?,?,?,?)",
                               [(user_id, key, value, int(time.time())) for key, value in serialized.items()])
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "Konto z tym adresem e-mail już istnieje. Zaloguj się."})
            return
        cookie = self.new_session(user_id)
        self.send_json(201, {"ok": True, "user": {"email": email}}, (cookie,))

    def login(self, payload: dict) -> None:
        email = str(payload.get("email", "")).strip().casefold()
        password = payload.get("password")
        allowed_email = os.environ.get("FITDIET_SINGLE_USER_EMAIL", "").strip().casefold()
        if not isinstance(password, str) or len(password) > 128:
            self.send_json(401, {"error": "Niepoprawny e-mail lub hasło."})
            return
        now = time.time()
        key = f"{self.client_address[0]}:{email}"
        failures = [stamp for stamp in FAILED_LOGINS.get(key, []) if now - stamp < 900]
        if len(failures) >= 8:
            self.send_json(429, {"error": "Za dużo prób logowania. Spróbuj ponownie za 15 minut."})
            return
        with database() as db:
            row = db.execute("SELECT id,email,password_salt,password_hash FROM users WHERE email=?", (email,)).fetchone()
            user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if (allowed_email and email != allowed_email) or (not allowed_email and user_count != 1):
            row = None
        if row:
            salt, expected = row["password_salt"], row["password_hash"]
        else:
            salt, expected = DUMMY_SALT, DUMMY_HASH
        actual = password_digest(password, salt)
        if not row or not hmac.compare_digest(actual, expected):
            failures.append(now)
            FAILED_LOGINS[key] = failures
            self.send_json(401, {"error": "Niepoprawny e-mail lub hasło."})
            return
        FAILED_LOGINS.pop(key, None)
        cookie = self.new_session(row["id"])
        self.send_json(200, {"ok": True, "user": {"email": row["email"]}}, (cookie,))

    def do_PUT(self) -> None:
        if urlsplit(self.path).path != "/api/state":
            self.send_json(404, {"error": "Nie znaleziono endpointu."})
            return
        if not self.require_write_origin():
            return
        user = self.session_user()
        if not user:
            self.send_json(401, {"error": "Sesja wygasła. Zaloguj się ponownie."})
            return
        payload = self.read_json()
        if payload is None:
            self.send_json(400, {"error": "Nieprawidłowe lub zbyt duże żądanie."})
            return
        values = payload.get("state")
        if values is None:
            key, value = payload.get("key"), payload.get("value")
            values = {key: value}
        if not isinstance(values, dict) or not values or not set(values).issubset(STATE_KEYS):
            self.send_json(400, {"error": "Nieprawidłowe dane do zapisu."})
            return
        now = int(time.time())
        try:
            serialized = [(user["id"], key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now)
                          for key, value in values.items()]
            if any(len(value.encode("utf-8")) > MAX_BODY for _, _, value, _ in serialized):
                raise ValueError
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Dane są nieprawidłowe lub zbyt duże."})
            return
        with database() as db:
            db.executemany("""
                INSERT INTO user_state(user_id,state_key,value_json,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(user_id,state_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at
            """, serialized)
        self.send_json(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        # Keep routine requests quiet; do not log emails, passwords, cookies, or user data.
        if self.command in {"POST", "PUT"} and self.path.startswith("/api/"):
            print(f"{self.address_string()} {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}")


def main() -> None:
    os.umask(0o077)
    init_db()
    configure_single_user()
    parser = argparse.ArgumentParser(description="Serwer Dobry Talerz z logowaniem i zapisem danych użytkowników.")
    parser.add_argument("--host", default=os.environ.get("FITDIET_HOST", "127.0.0.1"), help="Adres nasłuchu; 0.0.0.0 udostępnia aplikację w sieci lokalnej.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FITDIET_PORT", "8765")))
    args = parser.parse_args()
    host, port = args.host, args.port
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dobry Talerz nasłuchuje na http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano serwer.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
