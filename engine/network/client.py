"""
engine/network/client.py

Outbound dispatcher: encrypt a plaintext message and POST it to a
specific peer's listener. The matching receiver is in
``engine/mesh/server.py`` (the ``POST /api/messages/receive`` route).

Outbound flow per send:
  1. Save the message to our local DB first, so we have a record even
     if the network send fails. The local row's ``is_synced`` stays
     False (the partner's flag means "not yet pushed to Matrix" —
     local-mesh success does NOT promote it to True; the Matrix bridge
     is what flips that flag).
  2. Build the encrypted + signed bundle via ``prepare_outbound``.
  3. POST it to ``http://{peer.ip_address}:{peer.port}/api/messages/receive``
     with our own public keys attached, so the peer can verify the
     signature was made by us.
  4. On 200: return True. On any failure: log + return False. The
     message remains in the local DB with ``is_synced=False``; it can
     be retried by the caller, or picked up later by the Matrix bridge.

Failure handling matches AGENTS.md:
  - Narrow exception catches (``ConnectionError``, ``Timeout``) — we
    never let a dead peer crash the dispatcher.
  - Short timeout (``5s``) — a hung peer must not block the UI.
  - Try once, log, move on — no retry-with-backoff per scope.

We depend on:
  - ``engine.security.crypto.prepare_outbound`` (sign-then-encrypt).
  - ``engine.security.keys`` for our own signing + encryption keys
    (loaded on demand, same pattern as crypto.py).
  - ``engine.storage.database`` for the local save + unsynced query.
  - A ``Peer`` object (from ``engine.storage.models.Peer``) with at
    least ``ip_address``, ``port``, and ``public_key`` populated.
"""

import logging

import requests
from nacl.exceptions import CryptoError

from engine.security.crypto import prepare_outbound
from engine.security.keys import (
    load_or_create_encryption_key,
    load_or_create_signing_key,
)
from engine.storage import database

logger = logging.getLogger(__name__)

# Single hard-coded timeout for every outbound send. Per AGENTS.md:
# "a failed send marks the message is_synced=False; it never crashes
# or is silently dropped." 5s matches the skill's recipe and the
# plan's "dead peer doesn't hang your app" requirement.
REQUEST_TIMEOUT_SECONDS = 5


def _load_our_keypair():
    """Return (signing_key, encryption_key, verify_key_bytes, encryption_pubkey_bytes).

    Centralized so the wire payload and the local DB row reference the
    exact same key bytes — no chance of reading one key from disk and
    another from cache.
    """
    signing_key = load_or_create_signing_key()
    encryption_key = load_or_create_encryption_key()
    return (
        signing_key,
        encryption_key,
        signing_key.verify_key.encode().hex(),
        encryption_key.public_key.encode().hex(),
    )


def _save_outbound_locally(sender_id, content, signature=None):
    """Save the message to the default room so we have a record even if
    the network send fails. Returns the saved Message, or None if the
    default room doesn't exist / the sender isn't a member.

    The plan's 3.5 frames this as "mark undelivered" — we interpret it
    as "make sure the message exists locally with is_synced=False". The
    partner's ``is_synced`` field is about Matrix sync, not local-mesh
    delivery, so a locally-saved row is the right "unsent/unsynced"
    state regardless of whether the mesh send ultimately succeeds.
    """
    room = database.get_room_by_name("default")
    if room is None:
        logger.warning("no 'default' room; outbound not saved locally")
        return None
    saved = database.save_message(
        room_id=room.room_id,
        sender_id=sender_id,
        content=content,
        signature=signature,
    )
    if saved is None:
        # save_message returns None when the sender isn't a member of
        # the room. The caller is sending AS themselves, so they should
        # be a member. If they aren't, that's a setup problem we want
        # to surface, not silently swallow.
        logger.warning(
            "save_message returned None for sender=%s room=%s (not a member?)",
            sender_id, room.room_id,
        )
    return saved


def send_message(peer, plaintext: str, local_peer_id: str = "self") -> bool:
    """Encrypt ``plaintext`` for ``peer`` and POST it to their listener.

    Args:
        peer: a ``Peer`` instance (from ``engine.storage.models.Peer``).
            Must have ``ip_address``, ``port``, and ``public_key`` set.
            The ``public_key`` is treated as the peer's Box encryption
            public key — that's what the receiver's ``process_inbound``
            uses to open the bundle. (The peer's signing key isn't
            needed outbound; we sign with OURS and they verify with
            ours.)
        plaintext: the user-typed message string.
        local_peer_id: our own peer_id, used as the wire's ``sender_id``
            and for the local row's ``sender_id``. Defaults to ``"self"``
            for the test harness; the real app passes its discovery
            peer_id here.

    Returns:
        True on a 200 response from the peer.
        False on any network failure (timeout, connection refused,
        DNS error, bad URL, peer returned 4xx/5xx, or our own
        encryption step threw). The message will already be saved
        locally with ``is_synced=False``, so the caller can retry or
        leave it for the Matrix bridge.
    """
    # 1. Save locally first. If even this fails, the network send is
    #    pointless — return False so the UI can surface the error.
    local_row = _save_outbound_locally(
        sender_id=local_peer_id,
        content=plaintext,
    )
    if local_row is None:
        logger.error("could not save outbound locally; aborting send")
        return False

    # 2. Load our own keys (signing + encryption). The receiver needs
    #    our public verify_key + our public encryption key in the
    #    JSON body so they can open the box and check the signature.
    try:
        _, _, our_verify_key_hex, our_encryption_pubkey_hex = _load_our_keypair()
    except Exception as e:
        logger.exception("could not load our keypair: %s", e)
        return False

    # 3. Build the encrypted + signed bundle for the peer. If THIS
    #    fails (bad peer key bytes), bail before touching the network.
    try:
        from nacl.public import PublicKey
        peer_encryption_pubkey = PublicKey(bytes.fromhex(peer.public_key))
        ciphertext = prepare_outbound(plaintext, peer_encryption_pubkey)
    except (ValueError, CryptoError) as e:
        logger.error("could not build outbound bundle for %s: %s", peer.peer_id, e)
        return False

    # 4. POST to the peer's listener. Any failure here is non-fatal —
    #    the local row we just saved is the durable record.
    url = f"http://{peer.ip_address}:{peer.port}/api/messages/receive"
    payload = {
        "sender_public_key": our_encryption_pubkey_hex,
        "sender_verify_key": our_verify_key_hex,
        "sender_id": local_peer_id,
        "payload": ciphertext.hex(),
    }

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.ConnectionError:
        logger.warning("peer unreachable (connection refused): %s", url)
        return False
    except requests.exceptions.Timeout:
        logger.warning("peer timed out after %ss: %s",
                       REQUEST_TIMEOUT_SECONDS, url)
        return False
    except requests.exceptions.RequestException as e:
        # Catch-all for the rest of the requests exception hierarchy
        # (TooManyRedirects, InvalidURL, SSLError, etc.). Logging
        # the type is more useful than the message for these.
        logger.warning("peer send failed (%s): %s", type(e).__name__, e)
        return False

    if resp.status_code != 200:
        logger.warning(
            "peer rejected message: %s -> %d %s",
            url, resp.status_code, resp.text[:200],
        )
        return False

    return True


if __name__ == "__main__":
    # Quick manual smoke test: send to ourselves, hit our own listener.
    # Assumes mesh/server.py is already running on 127.0.0.1:5000 and
    # that the default room exists with "self" as a member.
    import sys
    from engine.storage import database
    from engine.storage.models import Peer

    # Make sure the default room + "self" membership exists. Idempotent.
    database.reset_db()
    room = database.create_room(
        creator_id="self",
        name="default",
        is_public=True,
        password_hash=None,
    )
    database.join_room(room_id=room.room_id, peer_id="self", password=None)

    me = Peer(
        peer_id="self",
        public_key=load_or_create_encryption_key().public_key.encode().hex(),
        name="self",
        ip_address="127.0.0.1",
        port=5000,
    )

    text = sys.argv[1] if len(sys.argv) > 1 else "hello, client.py"
    ok = send_message(me, text)
    print(f"send_message -> {ok}")
    sys.exit(0 if ok else 1)
