"""BYOK key storage: Fernet-encrypted JSON file.

Key derivation hierarchy:
1. Explicit VAULT_SECRET env var if provided.
2. Auto-generated persistent random key saved in ~/.coding-agent/.vault_key (chmod 600).
3. If neither (and in legacy fallback), derives from APP_PASSWORD or falls back to keys.json.

This keeps APP_PASSWORD strictly for web login authentication so changing
APP_PASSWORD never invalidates the encrypted keys vault.
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import secrets
from cryptography.fernet import Fernet, InvalidToken
from . import core

logger = logging.getLogger("ds_agent.crypto")
PLAIN_PATH = core.DATA_DIR / "keys.json"  # legacy/plaintext fallback


def _get_or_create_vault_secret() -> str:
    """Return persistent vault encryption secret string."""
    env_secret = os.environ.get("VAULT_SECRET", "").strip()
    if env_secret:
        return env_secret

    vault_key_file = getattr(core, "VAULT_KEY_PATH", core.DATA_DIR / ".vault_key")
    if vault_key_file.exists():
        try:
            content = vault_key_file.read_text().strip()
            if content:
                return content
        except Exception as e:
            logger.warning("Could not read vault key file %s: %s", vault_key_file, e)

    # Generate new random 32-byte hex secret and store with chmod 600
    new_secret = secrets.token_hex(32)
    try:
        vault_key_file.parent.mkdir(parents=True, exist_ok=True)
        vault_key_file.write_text(new_secret)
        try:
            os.chmod(vault_key_file, 0o600)
        except Exception:
            pass
        return new_secret
    except Exception as e:
        logger.warning("Could not persist vault key to %s: %s", vault_key_file, e)
        # Fallback to APP_PASSWORD if persistent file can't be created
        return core.APP_PASSWORD or "default-ds-agent-vault-secret"


def _fernet(secret: str | None = None) -> Fernet:
    s = secret if secret is not None else _get_or_create_vault_secret()
    seed = hashlib.sha256(s.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(seed))


def _read_all() -> dict:
    if not core.KEYS_PATH.exists():
        # Check if legacy plaintext keys.json exists
        if PLAIN_PATH.exists():
            try:
                data = json.loads(PLAIN_PATH.read_text() or "{}")
                if data:
                    _write_all(data)  # migrate to encrypted
                    return data
            except Exception:
                pass
        return {}

    raw = core.KEYS_PATH.read_bytes()
    if not raw:
        return {}

    # 1. Try standard vault secret / .vault_key
    f = _fernet()
    try:
        return json.loads(f.decrypt(raw).decode())
    except InvalidToken:
        logger.warning("Failed to decrypt keys.enc with VAULT_SECRET/.vault_key; attempting legacy APP_PASSWORD")

    # 2. Try legacy APP_PASSWORD derivation if different
    if core.APP_PASSWORD:
        try:
            f_legacy = _fernet(core.APP_PASSWORD)
            data = json.loads(f_legacy.decrypt(raw).decode())
            logger.info("Successfully decrypted keys.enc with legacy APP_PASSWORD; re-encrypting with new vault secret")
            _write_all(data)  # re-encrypt with current vault secret
            return data
        except InvalidToken:
            pass

    logger.error("Could not decrypt keys.enc with any known key; resetting corrupted/unreadable vault to {}")
    # Back up invalid token file instead of crashing server
    bak = core.DATA_DIR / "keys.enc.bak"
    try:
        core.KEYS_PATH.rename(bak)
    except Exception:
        pass
    return {}


def _write_all(d: dict) -> None:
    f = _fernet()
    core.KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    core.KEYS_PATH.write_bytes(f.encrypt(json.dumps(d).encode()))
    try:
        os.chmod(core.KEYS_PATH, 0o600)
    except Exception:
        pass


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
    return True
