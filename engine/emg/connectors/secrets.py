"""Connector credentials are encrypted at rest with CONNECTOR_SECRET_KEY (a Fernet key).
They are decrypted only inside the worker when a connector runs."""
from __future__ import annotations

import json
import os


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("CONNECTOR_SECRET_KEY")
    if not key:
        raise ValueError("CONNECTOR_SECRET_KEY is not set; generate one with: python -m emg.cli connector-key")
    return Fernet(key.encode())


def seal(creds: dict) -> bytes:
    return _fernet().encrypt(json.dumps(creds).encode())


def unseal(blob: bytes | memoryview | None) -> dict:
    if not blob:
        return {}
    return json.loads(_fernet().decrypt(bytes(blob)).decode())


def new_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def totp(secret: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 code for portals that require an authenticator app."""
    import base64
    import hashlib
    import hmac
    import struct
    import time

    k = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    h = hmac.new(k, struct.pack(">Q", int((at or time.time()) // step)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)
