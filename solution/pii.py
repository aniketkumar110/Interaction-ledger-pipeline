"""PII protection.

* Free-text PII (customer name, full phone) is stored only as a Fernet token
  (AES-128-CBC + HMAC) in ``leads.pii_enc``.
* Phone numbers are *matched* through a keyed hash (HMAC-SHA256). A plain
  SHA-256 of a 10-digit mobile number is brute-forceable in minutes (only ~4e9
  possibilities), so the key matters: without it the hashes are useless.
* Events never store the phone at all, only its keyed hash.

Key management: ``LEDGER_PII_KEY`` (a Fernet key) wins; otherwise a key file
``<db>.key`` is created next to the database (dev convenience only; in
production the key lives in a KMS/secret manager, never next to the data).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

ENV_KEY = "LEDGER_PII_KEY"


def load_key(db_path: str | Path) -> bytes:
    env = os.environ.get(ENV_KEY)
    if env:
        return env.encode()
    key_path = Path(str(db_path) + ".key")
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class PiiVault:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)
        self._hmac_key = hashlib.sha256(b"ledger-phone-hmac-v1" + key).digest()
        self.fingerprint = hashlib.sha256(b"ledger-key-fp-v1" + key).hexdigest()[:16]
        self._cache: dict[str, bytes] = {}

    def phone_hash(self, canonical_phone: str) -> bytes:
        h = self._cache.get(canonical_phone)
        if h is None:
            h = hmac.digest(self._hmac_key, canonical_phone.encode(), "sha256")
            if len(self._cache) < 500_000:
                self._cache[canonical_phone] = h
        return h

    def encrypt_json(self, obj: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(obj, sort_keys=True).encode())

    def decrypt_json(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))
