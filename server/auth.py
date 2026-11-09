"""Auth & server modes (§2): solo / team / accounts + onboarding state.

State machine (the server is the single source of truth):
  mode = 'setup'    fresh install — APIs stay OPEN like solo while the
                    onboarding wizard runs (§16: first entered = admin);
                    the frontend shows the wizard until `finish`.
  mode = 'solo'     auth OFF on the main UI; /admin still needs the owner's
                    session; provider keys stored plaintext (§7).
  mode = 'team'     every /api/* needs a session token; providers shared
                    in main.db; shared projects + concurrency limits (§2);
                    keys optionally sealed with the server master password.
  mode = 'accounts' login+password per user; each user gets an isolated
                    scope: <user>.db + own workspace; provider keys sealed
                    with a key derived from the user's password (§7).

Sessions live in memory only (restart = re-login) — that is what keeps the
accounts-mode key cipher non-persistable, i.e. encryption stays honest.
The session token travels as the `af_session` cookie (EventSource sends
cookies automatically) or as `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time

from .db import KeyCipher, SettingsStore
from .srvlog import LOGS

SESSION_COOKIE = "af_session"
SESSION_TTL = 7 * 24 * 3600
MODES = ("setup", "solo", "team", "accounts")

# routes that never require a session (auth itself + health + statics;
# statics are guarded by path prefix, not listed here)
PUBLIC_API = ("/api/health", "/api/auth/", "/api/onboarding/")


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=2 ** 14, r=8, p=1, dklen=32).hex()


def _hex(data: bytes) -> str:
    return data.hex()


def _unhex(text: str) -> bytes:
    return bytes.fromhex(text)


class AuthError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Session(dict):
    @property
    def user_id(self) -> str:
        return self["user_id"]

    @property
    def username(self) -> str:
        return self["username"]

    @property
    def role(self) -> str:
        return self["role"]

    @property
    def key(self) -> bytes | None:
        return self.get("_key")       # accounts: derived cipher key (never stored)


class Auth:
    def __init__(self, settings_store: SettingsStore):
        self.store = settings_store             # main.db — users live here
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    # -- mode ------------------------------------------------------------------

    async def mode(self) -> str:
        s = await self.store.get_settings()
        mode = s.get("mode", "solo")
        return mode if mode in MODES else "solo"

    async def needs_onboarding(self) -> bool:
        """§16: the wizard shows while the mode is not yet finalized."""
        return await self.mode() == "setup"

    async def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise AuthError(f"режим: {'/'.join(MODES)}")
        await self.store.set_settings({"mode": mode})
        LOGS.log_event("auth", f"режим сервера: {mode}")

    # -- users -----------------------------------------------------------------

    async def register(self, username: str, password: str,
                       role: str | None = None) -> dict:
        """First registered user becomes admin (§16)."""
        username = (username or "").strip()
        if not username or len(username) > 60:
            raise AuthError("имя пользователя: 1–60 символов")
        if not password or len(password) < 4:
            raise AuthError("пароль минимум 4 символа")
        async with self._lock:
            if await self.store.get_user_by_name(username):
                raise AuthError("имя уже занято")
            if role == "admin" and await self.store.count_users() > 0:
                # admins are only created by admins or as the first user
                role = "member"
            pw_salt = secrets.token_bytes(16)
            key_salt = secrets.token_bytes(16)
            user = await self.store.add_user(
                username, _hex(pw_salt), _scrypt(password, pw_salt),
                _hex(key_salt), role=role)
        LOGS.log_event("auth",
                       f"новый пользователь: {username} ({user['role']})")
        return user

    async def verify_login(self, username: str, password: str) -> Session:
        user = await self.store.get_user_by_name((username or "").strip())
        if not user or user.get("disabled"):
            raise AuthError("неверное имя или пароль", 401)
        try:
            expect = _scrypt(password, _unhex(user["pw_salt"]))
        except ValueError:
            raise AuthError("неверное имя или пароль", 401)
        if not secrets.compare_digest(expect, user["pw_hash"]):
            raise AuthError("неверное имя или пароль", 401)
        key = None
        if await self.mode() == "accounts":
            from . import crypto
            key = crypto.derive_key(password, _unhex(user.get("key_salt") or b""))
        token = secrets.token_urlsafe(32)
        session = Session(user_id=user["id"], username=user["username"],
                          role=user["role"], token=token,
                          expires=time.time() + SESSION_TTL, _key=key)
        self.sessions[token] = session
        LOGS.log_event("auth", f"вход: {user['username']}")
        return session

    def logout(self, token: str) -> None:
        s = self.sessions.pop(token, None)
        if s:
            LOGS.log_event("auth", f"выход: {s['username']}")

    def purge_expired(self) -> None:
        now = time.time()
        for token in [t for t, s in self.sessions.items()
                      if s.get("expires", 0) < now]:
            self.sessions.pop(token, None)

    # -- request resolution --------------------------------------------------------

    def session_for(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self.sessions.get(token)
        if not s or s.get("expires", 0) < time.time():
            return None
        return s

    async def check_request(self, token: str | None, path: str) -> Session | None:
        """Resolves the caller. In solo/setup a missing session maps to the
        implicit solo user; in team/accounts missing session on a protected
        route raises AuthError(401)."""
        session = self.session_for(token)
        mode = await self.mode()
        if session is not None:
            return session
        if mode in ("solo", "setup"):
            return Session(user_id="solo", username="solo", role="admin",
                           token="", expires=0, _key=None, implicit=True)
        if path.startswith(PUBLIC_API) or not path.startswith("/api"):
            return None                      # caller is simply not logged in
        raise AuthError("требуется вход", 401)

    # -- ciphers (§7) --------------------------------------------------------------

    def bind_user_cipher(self, session: Session, settings: SettingsStore) -> None:
        """Accounts mode: the scope's store gets the cipher derived from the
        user's password (only present while their session is alive)."""
        if session.get("_key"):
            settings.bind_cipher(KeyCipher(session["_key"]))

    async def set_master_password(self, password: str | None) -> dict:
        """Team mode §7: optional master password sealing the shared keys.
        Setting it re-seals every currently-plaintext key; clearing it
        decrypts them back (they must be readable first)."""
        from . import crypto
        providers = await self.store.list_providers()
        if password:
            salt_path = self.store.db_path + ".msalt"
            salt = crypto.load_or_create_salt(salt_path)
            key = crypto.derive_key(password, salt)
            cipher = KeyCipher(key)
            sealed = 0
            for p in providers:
                if p.get("api_key") and not p.get("key_sealed"):
                    env = cipher.encrypt(p["api_key"])
                    await self.store._db.execute(
                        "UPDATE providers SET api_key=? WHERE id=?",
                        (env, p["id"]))
                    sealed += 1
            await self.store._db.commit()
            await self.store.set_settings({"team_master_hash": _scrypt(
                password, salt)})
            self.store.bind_cipher(cipher)
            LOGS.log_event("auth",
                           f"мастер-пароль установлен; ключей запечатано: {sealed}")
            return {"sealed": sealed}
        # clearing -> decrypt everything back to plaintext (the cipher is
        # still bound here, so list_providers() returns decrypted keys)
        if self.store.cipher is None:
            return {"sealed": 0}
        unsealed = 0
        for p in await self.store.list_providers():
            if p.get("api_key"):
                await self.store._db.execute(
                    "UPDATE providers SET api_key=? WHERE id=?",
                    (p["api_key"], p["id"]))
                unsealed += 1
        await self.store._db.commit()
        await self.store.set_settings({"team_master_hash": ""})
        self.store.bind_cipher(None)
        LOGS.log_event("auth", f"мастер-пароль снят; ключей распечатано: {unsealed}")
        return {"unsealed": unsealed}

    async def unlock_master(self, password: str) -> bool:
        """Boot-time or admin unlock: binds the cipher until restart."""
        s = await self.store.get_settings()
        expect = s.get("team_master_hash") or ""
        if not expect:
            raise AuthError("мастер-пароль не установлен")
        from . import crypto
        salt_path = self.store.db_path + ".msalt"
        salt = crypto.load_or_create_salt(salt_path)
        if not secrets.compare_digest(_scrypt(password, salt), expect):
            return False
        self.store.bind_cipher(KeyCipher(
            crypto.derive_key(password, salt)))
        LOGS.log_event("auth", "мастер-пароль разблокирован")
        return True

    async def master_locked(self) -> bool:
        s = await self.store.get_settings()
        return bool(s.get("team_master_hash")) and self.store.cipher is None

    async def master_configured(self) -> bool:
        s = await self.store.get_settings()
        return bool(s.get("team_master_hash"))
