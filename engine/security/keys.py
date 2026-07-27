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

SIGNING_KEY_PATH = "identity_signing.key"
ENCRYPTION_KEY_PATH = "identity_encryption.key"


def load_or_create_signing_key() -> SigningKey:
    """Used to sign outbound messages (proves 'this really came from me')."""
    if os.path.exists(SIGNING_KEY_PATH):
        with open(SIGNING_KEY_PATH, "rb") as f:
            return SigningKey(f.read())

    key = SigningKey.generate()
    with open(SIGNING_KEY_PATH, "wb") as f:
        f.write(bytes(key))
    return key


def load_or_create_encryption_key() -> PrivateKey:
    """Used to encrypt/decrypt message contents."""
    if os.path.exists(ENCRYPTION_KEY_PATH):
        with open(ENCRYPTION_KEY_PATH, "rb") as f:
            return PrivateKey(f.read())

    key = PrivateKey.generate()
    with open(ENCRYPTION_KEY_PATH, "wb") as f:
        f.write(bytes(key))
    return key


if __name__ == "__main__":
    # Quick manual test: run this file twice.
    # First run: creates new keys. Second run: loads the SAME keys.
    signing_key = load_or_create_signing_key()
    encryption_key = load_or_create_encryption_key()

    print("Signing public key (share this):", signing_key.verify_key.encode().hex())
    print("Encryption public key (share this):", encryption_key.public_key.encode().hex())