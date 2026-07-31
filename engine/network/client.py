"""
engine/network/client.py

Outbound dispatcher: encrypt a plaintext message and POST it to a
specific peer's listener. The matching receiver is in
``engine/mesh/server.py`` (the ``POST /api/messages/receive`` route).

Multi-room routing (Phase 2):
  Outbound messages are wrapped in a JSON envelope
      {"room_id": "<uuid>", "content": "<text>"}
  BEFORE being passed to ``prepare_outbound``. The envelope is signed
  and encrypted as a single bundle, so a relaying peer can't tamper
  with either the room or the content without invalidating the
  signature. The server (mesh/server.py) parses the envelope on
  receipt to extract the room.

  The legacy single-room ``send_message(peer, text)`` is preserved as
  a thin wrapper around ``send_to_room`` for backwards compatibility
  with the existing test harness and __main__ block.

Outbound flow per send:
  1. Save the message to our local DB first, so we have a record even
     if the network send fails. The local row's ``is_synced`` stays
     False (the partner's flag means "not yet pushed to Matrix" —
     local-mesh success does NOT promote it to True; the Matrix bridge
     is what flips that flag).
  2. Build the envelope ``{"room_id": ..., "content": ...}`` and pass
     it to ``prepare_outbound`` for sign-then-encrypt.
  3. POST the resulting bundle to
     ``http://{peer.ip_address}:{peer.port}/api/messages/receive`` with
     our own public keys attached, so the peer can verify the
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

import json
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

# Join-result strings returned by the server's /api/rooms/join. Kept
# in sync with database.JoinResult.name values.
JOIN_RESULT_JOINED = "JOINED"
JOIN_RESULT_ALREADY_MEMBER = "ALREADY_MEMBER"
JOIN_RESULT_WRONG_PASSWORD = "WRONG_PASSWORD"
JOIN_RESULT_ROOM_NOT_FOUND = "ROOM_NOT_FOUND"


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


def _save_outbound_locally(sender_id, room_id, content, signature=None):
    """Save the message to the local DB so we have a record even if the
    network send fails. Returns the saved Message, or None if the room
    doesn't exist or the sender isn't a member.

    The plan's 3.5 frames this as "mark undelivered" — we interpret it
    as "make sure the message exists locally with is_synced=False". The
    partner's ``is_synced`` field is about Matrix sync, not local-mesh
    delivery, so a locally-saved row is the right "unsent/unsynced"
    state regardless of whether the mesh send ultimately succeeds.
    """
    saved = database.save_message(
        room_id=room_id,
        sender_id=sender_id,
        content=content,
        signature=signature,
    )
    if saved is None:
        # save_message returns None when the room doesn't exist or the
        # sender isn't a member. The caller is sending AS themselves, so
        # they should be a member. If they aren't, that's a setup
        # problem we want to surface, not silently swallow.
        logger.warning(
            "save_message returned None for sender=%s room=%s (not a member?)",
            sender_id, room_id,
        )
    return saved


def save_outbound(room_id: str, plaintext: str, local_peer_id: str):
    """Persist an outgoing message locally. Returns the saved Message,
    or None if the room doesn't exist / the sender isn't a member.

    Split from the network send so callers (the TUI) can save exactly
    once and then fan the bundle out to multiple peers without
    duplicating the local row. This is also the offline path: with no
    peers online, a saved-but-unsent message stays in the DB with
    ``is_synced=False`` — the Matrix bridge (or a future mesh
    redelivery pass) can pick it up later.
    """
    return _save_outbound_locally(
        sender_id=local_peer_id,
        room_id=room_id,
        content=plaintext,
    )


def send_bundle_to_peer(
    peer,
    room_id: str,
    plaintext: str,
    local_peer_id: str = "self",
) -> bool:
    """Encrypt ``plaintext`` for ``peer`` and POST it to their listener,
    addressing the message to ``room_id``. Does NOT save locally —
    callers persist via ``save_outbound`` first (exactly once), then
    fan out to as many peers as they like.

    Returns True on a 200 from the peer; False on any network or
    crypto failure. The local record (already saved by the caller) is
    the durable copy either way.
    """
    # 1. Load our own keys (signing + encryption). The receiver needs
    #    our public verify_key + our public encryption key in the
    #    JSON body so they can open the box and check the signature.
    try:
        _, _, our_verify_key_hex, our_encryption_pubkey_hex = _load_our_keypair()
    except Exception as e:
        logger.exception("could not load our keypair: %s", e)
        return False

    # 2. Wrap the plaintext in a JSON envelope with the room id, then
    #    pass the envelope to prepare_outbound. The envelope is what
    #    gets signed and encrypted — the server (mesh/server.py)
    #    decrypts and parses it to extract the room and content.
    try:
        from nacl.public import PublicKey
        peer_encryption_pubkey = PublicKey(bytes.fromhex(peer.public_key))
        envelope = json.dumps({"room_id": room_id, "content": plaintext})
        ciphertext = prepare_outbound(envelope, peer_encryption_pubkey)
    except (ValueError, CryptoError) as e:
        logger.error("could not build outbound bundle for %s: %s", peer.peer_id, e)
        return False

    # 3. POST to the peer's listener. Any failure here is non-fatal —
    #    the local row (saved by the caller) is the durable record.
    url = f"{peer.endpoint_url()}/api/messages/receive"
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
        logger.warning("peer send failed (%s): %s", type(e).__name__, e)
        return False

    if resp.status_code != 200:
        logger.warning(
            "peer rejected message: %s -> %d %s",
            url, resp.status_code, resp.text[:200],
        )
        return False

    return True


def send_to_room(
    peer,
    room_id: str,
    plaintext: str,
    local_peer_id: str = "self",
) -> bool:
    """Save locally, then encrypt + POST to a single peer.

    Convenience wrapper kept for callers that only ever talk to one
    peer (tests, the ``__main__`` smoke test). Multi-peer callers
    should use ``save_outbound`` + ``send_bundle_to_peer`` instead so
    the local row is written exactly once.
    """
    local_row = save_outbound(room_id, plaintext, local_peer_id)
    if local_row is None:
        logger.error("could not save outbound locally; aborting send")
        return False
    return send_bundle_to_peer(peer, room_id, plaintext, local_peer_id)


def send_message(peer, plaintext: str, local_peer_id: str = "self") -> bool:
    """Backwards-compat shim: send ``plaintext`` to the "default" room.

    Equivalent to ``send_to_room(peer, <default_room_id>, ...)``. Kept
    so existing test harnesses and the ``__main__`` block continue to
    work without modification. New callers should use
    ``send_to_room`` directly with an explicit room_id.
    """
    room = database.get_room_by_name("default")
    if room is None:
        logger.error("no 'default' room; cannot send_message")
        return False
    return send_to_room(peer, room.room_id, plaintext, local_peer_id)


def list_remote_rooms(peer) -> list[dict]:
    """GET ``/api/rooms`` from ``peer`` and return the parsed list.

    Returns an empty list on any failure (timeout, non-200, malformed
    JSON) so the caller can treat "couldn't reach the peer" and "peer
    has no public rooms" the same way. Per-room error logging happens
    here; the caller just decides what to do with the empty result.
    """
    url = f"{peer.endpoint_url()}/api/rooms"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.ConnectionError:
        logger.warning("list_remote_rooms: peer unreachable: %s", url)
        return []
    except requests.exceptions.Timeout:
        logger.warning("list_remote_rooms: timed out: %s", url)
        return []
    except requests.exceptions.RequestException as e:
        logger.warning("list_remote_rooms: failed (%s): %s", type(e).__name__, e)
        return []

    if resp.status_code != 200:
        logger.warning(
            "list_remote_rooms: peer returned %d: %s",
            resp.status_code, resp.text[:200],
        )
        return []
    try:
        return resp.json()
    except ValueError:
        logger.warning("list_remote_rooms: unparseable JSON")
        return []


def join_remote_room(
    peer,
    room_id: str,
    local_peer_id: str,
    password=None,
) -> str:
    """POST to ``/api/rooms/join`` on ``peer`` to add ourselves to ``room_id``.

    Returns the JoinResult name as a string: one of
    ``"JOINED"``, ``"ALREADY_MEMBER"``, ``"WRONG_PASSWORD"``,
    ``"ROOM_NOT_FOUND"``, or ``"ERROR"`` on transport failure. The
    caller maps these to UI feedback.
    """
    url = f"{peer.endpoint_url()}/api/rooms/join"
    payload = {
        "room_id": room_id,
        "sender_id": local_peer_id,
        "password": password,
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as e:
        logger.warning("join_remote_room: failed (%s): %s", type(e).__name__, e)
        return "ERROR"

    if resp.status_code != 200:
        logger.warning(
            "join_remote_room: peer returned %d: %s",
            resp.status_code, resp.text[:200],
        )
        return "ERROR"
    try:
        return resp.json().get("result", "ERROR")
    except ValueError:
        return "ERROR"


def pull_room_history(
    peer,
    room_id: str,
    local_peer_id: str,
    since: float = 0.0,
) -> list[dict]:
    """GET messages from ``/api/rooms/{room_id}/messages?since=...``.

    Returns a list of message dicts (``message_id``, ``sender_id``,
    ``content``, ``timestamp``). Returns ``[]`` on any failure; the
    caller is expected to log/display as appropriate.
    """
    url = (
        f"{peer.endpoint_url()}"
        f"/api/rooms/{room_id}/messages"
        f"?sender_id={local_peer_id}&since={since}"
    )
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as e:
        logger.warning("pull_room_history: failed (%s): %s", type(e).__name__, e)
        return []
    if resp.status_code != 200:
        logger.warning(
            "pull_room_history: peer returned %d: %s",
            resp.status_code, resp.text[:200],
        )
        return []
    try:
        return resp.json().get("messages", [])
    except ValueError:
        return []


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
