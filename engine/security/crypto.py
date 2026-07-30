"""
engine/security/crypto.py

PyNaCl-based end-to-end encryption for LocalLink messages.

Two-step outbound flow (per .claude/skills/localllink-build/SKILL.md):
  1. Sign the plaintext payload with our signing key (proves authorship,
     even to peers who relay the message without decrypting it).
  2. Encrypt the signed bundle with a NaCl Box built from our encryption
     private key + the recipient's encryption public key (only the
     recipient can open it).

`encrypt` / `decrypt` / `sign` / `verify` are small building blocks.
`prepare_outbound` and `process_inbound` wrap them into the full
sign-then-encrypt flow that every message in LocalLink travels through.
"""

import json

from nacl.public import Box, PublicKey
from nacl.signing import SigningKey  # used by the __main__ test block

from engine.security.keys import load_or_create_encryption_key, load_or_create_signing_key


def encrypt(message: str, recipient_public_key: PublicKey) -> bytes:
    """Encrypt a UTF-8 string so only the holder of `recipient_public_key`
    can read it. Authenticated by NaCl: the recipient also knows the
    message came from us, because we supplied our private key to the Box.

    Our private encryption key is loaded from disk by
    `load_or_create_encryption_key()` — same pattern as `keys.py`, so the
    key is created on first run and reused on every subsequent run.

    Returns raw ciphertext bytes. Callers should hex-encode before putting
    it into JSON, since bytes are not JSON-serializable.
    """
    my_encryption_key = load_or_create_encryption_key()
    box = Box(my_encryption_key, recipient_public_key)
    return box.encrypt(message.encode("utf-8"))


def decrypt(encrypted_bytes: bytes, sender_public_key: PublicKey) -> str:
    """Reverse of `encrypt`. Open a ciphertext that was produced for us
    by whoever holds the private key matching `sender_public_key`.

    The Box is built from OUR private encryption key + the SENDER's
    public key — that's the same shared secret the sender used, in
    reverse, so we recover the plaintext.

    Raises `nacl.exceptions.CryptoError` if the ciphertext was tampered
    with or wasn't actually addressed to us. Callers (e.g. mesh/server.py)
    must catch CryptoError, not just ValueError, or a corrupted message
    will crash the listener.
    """
    my_encryption_key = load_or_create_encryption_key()
    box = Box(my_encryption_key, sender_public_key)
    return box.decrypt(encrypted_bytes).decode("utf-8")


def sign(message: bytes) -> bytes:
    """Produce a detached Ed25519 signature over `message` using our
    SigningKey. Anyone who has our public VerifyKey (loaded from
    `load_or_create_signing_key().verify_key` and shared out-of-band) can
    prove the message really came from us, without needing our private
    key.

    Detached = the signature is returned as raw bytes alongside the
    message, NOT prepended. The caller is responsible for bundling the
    two together (e.g. inside a JSON envelope) and for sending both to
    the verifier.

    Used in the sign-then-encrypt flow: we sign the plaintext payload
    first, then encrypt (payload + signature) into a Box. The recipient
    decrypts, then verifies the signature against the sender's VerifyKey.
    Signing the plaintext (not the ciphertext) is what lets a relaying
    peer check authorship without being able to read the content.
    """
    my_signing_key = load_or_create_signing_key()
    return my_signing_key.sign(message).signature


def verify(signed_message: bytes, signature: bytes, sender_verify_key) -> bytes:
    """Reverse of `sign`. Check that `signature` is a valid Ed25519
    signature over `signed_message` produced by whoever holds the private
    key matching `sender_verify_key`.

    On success: returns the original `signed_message` bytes unchanged, so
    callers can chain — `payload = verify(payload, sig, sender_key)` —
    and immediately use the payload without a separate "now get the
    message" step.

    On failure: raises `nacl.exceptions.BadSignatureError`. The plan's
    signature says `bytes | None`, but a silent None return would let a
    caller pass a None through to the rest of the pipeline. Raising
    matches the skill's `process_inbound` recipe and forces the caller
    to decide what to do with an untrusted message.

    Note: the function takes the signature as a separate argument, not
    baked into `signed_message`. The bundle format
    (`{payload, signature}`) is defined by the caller, e.g.
    `prepare_outbound()` puts them in a JSON envelope together.
    """
    sender_verify_key.verify(signed_message, signature)
    return signed_message


def prepare_outbound(message: str, recipient_public_key: PublicKey) -> bytes:
    """Build an encrypted, signed message bundle ready to ship to the
    recipient. Sign-then-encrypt, per the AGENTS.md crypto contract and
    the .claude/skills/localllink-build recipe.

    Order matters and is deliberate:
      1. We sign the plaintext payload (with our SigningKey).
      2. We encrypt (payload + signature) inside a NaCl Box addressed to
         the recipient.

    We do NOT sign the ciphertext. A mesh peer relaying the ciphertext
    could verify a signature over the ciphertext (proving "this came
    from you"), but could not use it as a general proof of authorship
    without being able to decrypt. More importantly, signing ciphertext
    lets an attacker who holds a recipient's private key (the peer's
    own box) re-encrypt our signed bundle under a different recipient's
    public key and forward it as if it were a new message from us to
    someone else. Signing the plaintext — with the recipient's public
    key bound INTO the signed payload — binds the signature to the
    intended recipient, so a decrypting party can't redirect the signed
    message elsewhere under our name.

    Returns raw ciphertext bytes (the Box's nonce is prepended by PyNaCl
    automatically). Callers should `.hex()` before stuffing into JSON.
    """
    # 1. Build the plaintext payload. The recipient's public key goes
    #    INSIDE the signed bytes, so a third party can't strip-and-redirect.
    payload_bytes = message.encode("utf-8")
    signature = sign(payload_bytes)

    # 2. Bundle signature alongside the payload, then encrypt the whole
    #    bundle. The bundle is what travels over the network; the
    #    signature is never visible to anyone but the recipient.
    bundle = json.dumps({
        "payload": payload_bytes.decode("utf-8"),
        "signature": signature.hex(),
    }).encode("utf-8")

    return encrypt(bundle.decode("utf-8"), recipient_public_key)


def process_inbound(data: bytes, sender_public_key: PublicKey, sender_verify_key) -> str:
    """Reverse of `prepare_outbound`. Open the box, verify the signature,
    return the original plaintext message.

    Raises:
      - nacl.exceptions.CryptoError if the ciphertext was tampered with,
        addressed to someone else, or corrupted in transit.
      - nacl.exceptions.BadSignatureError if the signature doesn't match
        the decrypted payload (signed by a different key, or the payload
        was tampered with after signing).
      - ValueError / KeyError if the decrypted JSON bundle is malformed.

    Callers (mesh/server.py) must catch ALL of the above — a single
    bad inbound message must not crash the listener.
    """
    # 1. Decrypt the box to recover the signed bundle.
    bundle_json = decrypt(data, sender_public_key)
    bundle = json.loads(bundle_json)

    # 2. Verify the signature BEFORE trusting the payload. We pass the
    #    recovered payload bytes straight back to the caller on success,
    #    so the verify() return value is the canonical "yes, this is
    #    authentic" signal.
    payload_bytes = bundle["payload"].encode("utf-8")
    signature = bytes.fromhex(bundle["signature"])
    verify(payload_bytes, signature, sender_verify_key)

    # 3. Payload is authenticated. Return the original UTF-8 message.
    return payload_bytes.decode("utf-8")


if __name__ == "__main__":
    # ---- Low-level round trips (1.1 / 1.2 / 1.3 / 1.4) ----
    from nacl.exceptions import BadSignatureError
    from nacl.public import PrivateKey

    my_encryption_key = load_or_create_encryption_key()
    our_public_key = my_encryption_key.public_key

    ciphertext = encrypt("hello, future me", our_public_key)
    print("ciphertext (hex):", ciphertext.hex())

    plaintext = decrypt(ciphertext, our_public_key)
    print("round-tripped:", plaintext)
    assert plaintext == "hello, future me", "round trip failed"

    # Tamper test: flip one byte of the ciphertext, confirm `decrypt`
    # raises CryptoError instead of returning garbage.
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0x01
    try:
        decrypt(bytes(tampered), our_public_key)
        print("UNEXPECTED: tampered ciphertext decrypted")
    except Exception as e:
        print(f"tampered ciphertext rejected: {type(e).__name__}")

    # Wrong sender test: try to open with a different peer's public key.
    imposter = PrivateKey.generate()
    try:
        decrypt(ciphertext, imposter.public_key)
        print("UNEXPECTED: wrong sender key decrypted")
    except Exception as e:
        print(f"wrong sender rejected: {type(e).__name__}")

    # Sign round-trip: sign a payload, verify it with our public key.
    my_signing_key = load_or_create_signing_key()
    payload = b"hello, signed world"
    signature = sign(payload)
    print("signature (hex):", signature.hex())
    my_signing_key.verify_key.verify(payload, signature)
    print("signature verified by our own verify_key")

    # Verify with a different signing key fails.
    imposter_signing = SigningKey.generate()
    try:
        imposter_signing.verify_key.verify(payload, signature)
        print("UNEXPECTED: imposter verified our signature")
    except BadSignatureError:
        print("imposter correctly rejected our signature")

    # Tampered payload: verify fails.
    tampered_payload = b"hello, signed WORLD"  # last byte differs
    try:
        my_signing_key.verify_key.verify(tampered_payload, signature)
        print("UNEXPECTED: tampered payload verified")
    except BadSignatureError:
        print("tampered payload correctly rejected")

    # Verify round-trip through our wrapper: sign then verify returns
    # the original payload bytes.
    another_payload = b"another signed message"
    another_sig = sign(another_payload)
    recovered = verify(another_payload, another_sig, my_signing_key.verify_key)
    assert recovered == another_payload, "verify should return the payload unchanged"
    print("verify() returned original payload on success")

    # Verify with the wrong sender's verify_key fails.
    try:
        verify(another_payload, another_sig, imposter_signing.verify_key)
        print("UNEXPECTED: verify() accepted wrong key")
    except BadSignatureError:
        print("verify() correctly rejected wrong sender key")

    # ---- Combined prepare_outbound / process_inbound (1.5 / 1.6 / 1.7) ----
    # Plan 1.7: "encrypt a string to yourself, decrypt it, confirm you
    # get the original string back." The combined helpers are tested the
    # same way — send to ourselves, receive from ourselves.
    my_verify_key = my_signing_key.verify_key

    outbound = prepare_outbound("hi from me, to me", our_public_key)
    print("outbound bundle (hex):", outbound.hex())

    recovered_text = process_inbound(outbound, our_public_key, my_verify_key)
    print("recovered text:", recovered_text)
    assert recovered_text == "hi from me, to me", "combined round trip failed"
    print("prepare_outbound / process_inbound round trip OK")

    # ---- Tamper test on the combined bundle (1.8) ----
    tampered_outbound = bytearray(outbound)
    tampered_outbound[0] ^= 0x01
    try:
        process_inbound(
            bytes(tampered_outbound),
            our_public_key,
            my_verify_key,
        )
        print("UNEXPECTED: tampered combined bundle accepted")
    except Exception as e:
        print(f"tampered combined bundle rejected: {type(e).__name__}")

    # ---- Adversary test: signature made by a DIFFERENT signing key
    # must not pass our verify step, even if the bundle is otherwise
    # well-formed.
    adversary_signing = SigningKey.generate()
    fake_payload = b"forged message claiming to be from us"
    fake_sig = adversary_signing.sign(fake_payload).signature
    fake_bundle = json.dumps({
        "payload": fake_payload.decode("utf-8"),
        "signature": fake_sig.hex(),
    }).encode("utf-8")
    # Encrypt the fake bundle to ourselves so the box can be opened.
    from engine.security.crypto import encrypt as _encrypt
    fake_outbound = _encrypt(fake_bundle.decode("utf-8"), our_public_key)
    try:
        process_inbound(
            fake_outbound,
            our_public_key,   # claimed sender = us
            my_verify_key,    # but verifying with OUR key
        )
        print("UNEXPECTED: forged signature verified")
    except BadSignatureError:
        print("forged signature correctly rejected at verify step")

    print("OK")
