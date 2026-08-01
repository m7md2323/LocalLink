"""
engine/security/keys.py

Generates a permanent identity (public/private keypair) for this device
on first run, and reloads the same identity on every future run.

Why we need two separate keys:
- SigningKey (private) + VerifyKey (public): used to PROVE a message came
  from you (signing). Nobody can fake your signature without your private key.
- PrivateKey (private) + PublicKey (public), from nacl.public: used to
  ENCRYPT messages so only the intended recipient can read them.

We generate BOTH pairs because signing and encryption are different jobs.
"""

import os
import sys
from pathlib import Path

from nacl.signing import SigningKey
from nacl.public import PrivateKey


def _default_keys_dir() -> Path:
    """Return the stable location for the key files.

    When running as a frozen .exe, state lives NEXT TO the .exe so the
    whole folder is portable (copy it to a USB stick, zip it up, etc.).

    When running from source (``python run.py``), state lives in CWD
    (the repo root), matching the developer workflow.
    """
    if getattr(sys, "frozen", False) and getattr(sys, "executable", None):
        return Path(sys.executable).parent / "keys"
    return Path("keys")  # dev: CWD = repo root


def _resolve_keys_dir() -> Path:
    """Resolve the keys directory.

    Rules:
      - An ABSOLUTE path in ``LOCALLINK_KEYS_DIR`` is honored as-is.
      - A relative path is ignored (it would re-introduce the
        "different CWD = different identity" bug the defaults were
        added to prevent).
      - If unset, fall back to the platform-appropriate default.
    """
    override = os.environ.get("LOCALLINK_KEYS_DIR", "").strip()
    if override:
        override_path = Path(override)
        if override_path.is_absolute():
            return override_path
    return _default_keys_dir()


_KEYS_DIR = _resolve_keys_dir()
SIGNING_KEY_PATH = _KEYS_DIR / "identity_signing.key"
ENCRYPTION_KEY_PATH = _KEYS_DIR / "identity_encryption.key"


def _load_or_create(path: Path, factory):
    """Load a key from ``path`` if it exists, else generate + persist one."""
    if path.exists():
        with open(path, "rb") as f:
            return factory(f.read())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = factory.generate()
    with open(path, "wb") as f:
        f.write(bytes(key))
    return key


def load_or_create_signing_key() -> SigningKey:
    """Used to sign outbound messages (proves 'this really came from me')."""
    return _load_or_create(SIGNING_KEY_PATH, SigningKey)


def load_or_create_encryption_key() -> PrivateKey:
    """Used to encrypt/decrypt message contents."""
    return _load_or_create(ENCRYPTION_KEY_PATH, PrivateKey)


if __name__ == "__main__":
    # Quick manual test: run this file twice.
    # First run: creates new keys. Second run: loads the SAME keys.
    signing_key = load_or_create_signing_key()
    encryption_key = load_or_create_encryption_key()

    print("Keys directory:", _KEYS_DIR)
    print("Signing public key (share this):", signing_key.verify_key.encode().hex())
    print("Encryption public key (share this):", encryption_key.public_key.encode().hex())
