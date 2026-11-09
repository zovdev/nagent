"""Key encryption (§7).

- solo mode: provider API keys sit in main.db as-is (single user, local).
- accounts mode: each user's keys live in <user>.db ENCRYPTED with a key
  derived from the user's password (PBKDF2-HMAC-SHA256, 240k iterations).
- team mode: optional server master password encrypts the shared keys.

Cipher: AES-256-GCM when the `cryptography` package is available; otherwise
an authenticated CTR keystream built from HMAC-SHA256 (stdlib only — the
framework keeps zero hard dependencies beyond aiohttp/aiosqlite/httpx).
Both are encrypt-then-MAC constructions with random nonces; both produce
the same `enc:v1:<base64>` envelope so databases stay portable between
installs with/without `cryptography`.

No plaintext key ever leaves this module: call sites store/see envelopes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_VERSION = "v1"
_PREFIX = f"enc:{_VERSION}:"
PBKDF2_ROUNDS = 240_000


class CryptoError(RuntimeError):
    pass


# --------------------------------------------------------------------- keys

def derive_key(secret: str | bytes, salt: bytes, rounds: int = PBKDF2_ROUNDS) -> bytes:
    """Password -> 32-byte key (PBKDF2-HMAC-SHA256). Same salt+secret always
    derives the same key — the salt is stored alongside the envelope."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", secret, salt, rounds)


def random_salt(n: int = 16) -> bytes:
    return os.urandom(n)


def is_envelope(value: str | None) -> bool:
    return bool(value) and isinstance(value, str) and value.startswith(_PREFIX)


# --------------------------------------------------------------------- cipher

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _split(secret: bytes, n: int) -> tuple[bytes, bytes]:
    """Two independent subkeys from one master key (enc key + mac key)."""
    enc = hmac.new(secret, b"enc", hashlib.sha256).digest()
    mac = hmac.new(secret, b"mac", hashlib.sha256).digest()
    return enc[:n], mac


def _keystream_hmac(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """CTR-style keystream: HMAC-SHA256(enc_key, nonce || counter) blocks."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(key: bytes, plaintext: str) -> str:
    """Envelope: enc:v1:<salt>:<nonce>:<ct>:<tag>. AES-GCM if available,
    else HMAC-CTR + HMAC tag (encrypt-then-MAC)."""
    data = plaintext.encode("utf-8")
    nonce = os.urandom(12)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        ct = AESGCM(key).encrypt(nonce, data, None)   # ct includes the tag
        return f"{_PREFIX}{_b64(nonce)}:{_b64(ct)}"
    except ImportError:
        enc_key, mac_key = _split(key, 32)
        stream = _keystream_hmac(enc_key, nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:16]
        return f"{_PREFIX}{_b64(nonce)}:{_b64(ct)}:{_b64(tag)}"


def decrypt(key: bytes, envelope: str) -> str:
    if not is_envelope(envelope):
        # legacy plaintext (solo db opened under a later-set master password)
        return envelope
    parts = envelope[len(_PREFIX):].split(":")
    try:
        if len(parts) == 2:                     # AES-GCM form
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as e:
                raise CryptoError("конверт AES-GCM, а пакет cryptography "
                                  "не установлен") from e
            nonce, ct = _unb64(parts[0]), _unb64(parts[1])
            try:
                data = AESGCM(key).decrypt(nonce, ct, None)
            except Exception as e:              # InvalidTag — wrong key / corrupted
                raise CryptoError(f"не удалось расшифровать ({type(e).__name__}): "
                                  "неверный пароль или повреждённые данные") from e
        elif len(parts) == 3:                   # HMAC-CTR form
            enc_key, mac_key = _split(key, 32)
            nonce, ct, tag = (_unb64(p) for p in parts)
            expect = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(tag, expect):
                raise CryptoError("integrity check failed — неверный пароль "
                                  "или повреждённые данные")
            stream = _keystream_hmac(enc_key, nonce, len(ct))
            data = bytes(a ^ b for a, b in zip(ct, stream))
        else:
            raise CryptoError("неподдерживаемый формат конверта")
    except ValueError as e:
        raise CryptoError(f"не удалось расшифровать: {e}") from e
    return data.decode("utf-8")


# --------------------------------------------------------------------- salt store

def load_or_create_salt(path: str) -> bytes:
    """Stable per-database salt: the KDF salt lives next to the db so the
    same password + same salt keeps deriving the same key across restarts."""
    if os.path.isfile(path):
        with open(path, "rb") as f:
            salt = f.read()
        if len(salt) >= 8:
            return salt
    salt = random_salt()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt


# --------------------------------------------------------------------- self-test

def _selftest() -> None:
    key = derive_key("horse battery staple", b"0123456789abcdef")
    for _ in range(3):
        env = encrypt(key, "sk-secret-ключ-🔑")
        assert is_envelope(env)
        assert decrypt(key, env) == "sk-secret-ключ-🔑"
    other = derive_key("wrong", b"0123456789abcdef")
    try:
        decrypt(other, env)
        raise AssertionError("wrong key must not decrypt")
    except CryptoError:
        pass


_selftest()
