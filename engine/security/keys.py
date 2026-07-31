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
from nacl.signing import SigningKey
from nacl.public import PrivateKey

# Keys live in LOCALLINK_KEYS_DIR when set (default: current directory).
# .env ships with ".keys" so the repo root stays clean and the keys are
# covered by the existing .gitignore entry for that directory.
_KEYS_DIR = os.environ.get("LOCALLINK_KEYS_DIR", "").strip() or "."

SIGNING_KEY_PATH = os.path.join(_KEYS_DIR, "identity_signing.key")
ENCRYPTION_KEY_PATH = os.path.join(_KEYS_DIR, "identity_encryption.key")


def _load_or_create(path: str, factory):
    """Load a key from ``path`` if it exists, else generate + persist one."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            return factory(f.read())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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

    print("Signing public key (share this):", signing_key.verify_key.encode().hex())
    print("Encryption public key (share this):", encryption_key.public_key.encode().hex())