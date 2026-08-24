"""BYOK key storage: Fernet-encrypted JSON file. Key derived from APP_PASSWORD.

Falls back to plaintext JSON when APP_PASSWORD is empty (single-user, localhost
mode). The UI surfaces a warning so the user knows to set APP_PASSWORD when
they expose the server to a public host.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from cryptography.fernet import Fernet
from . import core

PLAIN_PATH = core.DATA_DIR / "keys.json"  # used when APP_PASSWORD is empty

def _fernet() -> Fernet | None:
    pw = core.APP_PASSWORD
    if not pw:
        return None
    seed = hashlib.sha256(pw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(seed))

def _read_all() -> dict:
    f = _fernet()
    if f is None:
        # plaintext fallback
        if not PLAIN_PATH.exists():
            return {}
        return json.loads(PLAIN_PATH.read_text() or "{}")
    if not core.KEYS_PATH.exists():
        return {}
    raw = core.KEYS_PATH.read_bytes()
    if not raw:
        return {}
    return json.loads(f.decrypt(raw).decode())

def _write_all(d: dict) -> None:
    f = _fernet()
    if f is None:
        PLAIN_PATH.write_text(json.dumps(d, indent=2))
        # make sure the file is user-only on POSIX
        try: os.chmod(PLAIN_PATH, 0o600)
        except Exception: pass
    else:
        core.KEYS_PATH.write_bytes(f.encrypt(json.dumps(d).encode()))

def save_key(provider: str, key: str, base_url: str | None = None, label: str | None = None) -> None:
    d = _read_all()
    d[provider] = {"key": key, "base_url": base_url, "label": label or provider}
    _write_all(d)

def load_key(provider: str) -> dict | None:
    return _read_all().get(provider)

def list_providers() -> list[str]:
    return list(_read_all().keys())

def delete_key(provider: str) -> None:
    d = _read_all()
    d.pop(provider, None)
    _write_all(d)

def is_encrypted() -> bool:
    return core.APP_PASSWORD != ""
