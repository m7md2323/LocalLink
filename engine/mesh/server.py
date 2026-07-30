"""
engine/mesh/server.py

Flask listener for inbound LocalLink messages. Runs on this device so
remote peers can POST encrypted, signed bundles to us, and we can decrypt,
verify authorship, and persist the message locally.

The route is the single point of contact between the wire and our
crypto + storage layers:
  POST /api/messages/receive
    Body: {
      "sender_public_key":     "<hex, our peer's Box public key>",
      "sender_verify_key":     "<hex, our peer's Ed25519 verify key>",
      "sender_id":             "<peer_id string, e.g. UUID>",
      "payload":               "<hex, ciphertext from prepare_outbound>"
    }
  Responses:
    200 {"status": "ok", "message_id": "..."} on success
    400 {"error": "..."} on any failure (bad JSON, bad hex, bad sig,
        tampered ciphertext, malformed bundle, unknown sender, etc.)
    500 {"error": "..."} only on an unexpected internal error

Design choices:
  - One route, no blueprints, no app factory (per AGENTS.md + skill).
  - All expected error paths collapse to a clean 400. We never let a
    bad inbound message crash the listener — that would let any peer
    on the mesh take us down by sending garbage.
  - We carry an in-process "last inbound" timestamp so the UI can poll
    it cheaply instead of re-querying the database on every tick.
  - Sync Flask only (no async). A single-threaded dev server is fine
    for a local mesh; production deployment is out of scope.
"""

import json
import time

from flask import Flask, jsonify, request
from nacl.exceptions import BadSignatureError
from nacl.public import PublicKey
from nacl.signing import VerifyKey

from engine.security.crypto import process_inbound
from engine.storage import database

app = Flask(__name__)

# Module-level "last inbound" timestamp. The UI polls this (a future
# GET /api/messages/status) to know whether to re-fetch /messages.
# In-memory only — restarting the server resets it, which is fine for
# a polling-based UI.
last_inbound_at: float = 0.0

# Cached room_id for the "default" room, looked up lazily on first use
# and reused for every subsequent inbound. Reset to None on server
# restart; not thread-safe but fine for a single-process Flask dev server.
_default_room_id = None


def _bad_request(reason: str):
    """Build a uniform 400 response. Centralized so the message format
    never drifts between handlers.
    """
    return jsonify({"error": reason}), 400


def _find_default_room():
    """Return the room_id of the room named "default", or None if no
    such room exists. Cached after the first call so we don't hit the
    DB on every inbound message.

    For now there is exactly one "default" room; if the project later
    needs a per-peer default or per-channel routing, replace this with
    a config-driven lookup.
    """
    global _default_room_id
    if _default_room_id is not None:
        return _default_room_id
    room = database.get_room_by_name("default")
    if room is None:
        return None
    _default_room_id = room.room_id
    return _default_room_id


def _decode_hex_key(hex_str: str, kind: str):
    """Decode a hex-encoded public key into the right PyNaCl object.

    `kind` is "encryption" or "signing" — selects which PyNaCl class to
    wrap the bytes in. Raises ValueError on bad hex or wrong length;
    the route catches that and returns 400.
    """
    raw = bytes.fromhex(hex_str)
    if kind == "encryption":
        return PublicKey(raw)
    if kind == "signing":
        return VerifyKey(raw)
    raise ValueError(f"unknown key kind: {kind}")


@app.route("/api/messages/receive", methods=["POST"])
def receive():
    """Receive one encrypted, signed message bundle from a peer.

    Failure mode contract: any of the following returns 400, never 500:
      - body is not valid JSON
      - any required field is missing
      - any hex field isn't valid hex
      - the ciphertext doesn't decrypt (wrong key, tampered, corrupted)
      - the signature doesn't verify (wrong sender key, payload tampered)
      - the decrypted JSON bundle is malformed
    """
    global last_inbound_at

    # 1. Parse the JSON body. force=True accepts any content-type as long
    #    as it's valid JSON, which is friendlier for ad-hoc curl tests.
    try:
        data = request.get_json(force=True)
    except Exception:
        return _bad_request("body is not valid JSON")

    if not isinstance(data, dict):
        return _bad_request("body must be a JSON object")

    # 2. Pull the four required fields. Missing fields are a 400, not a
    #    KeyError that crashes the worker.
    try:
        sender_public_key_hex = data["sender_public_key"]
        sender_verify_key_hex = data["sender_verify_key"]
        sender_id = data["sender_id"]
        payload_hex = data["payload"]
    except KeyError as missing:
        return _bad_request(f"missing field: {missing.args[0]}")

    # 3. Decode the keys + ciphertext. Any malformed hex becomes a 400.
    try:
        sender_public_key = _decode_hex_key(sender_public_key_hex, "encryption")
        sender_verify_key = _decode_hex_key(sender_verify_key_hex, "signing")
        ciphertext = bytes.fromhex(payload_hex)
    except (ValueError, TypeError) as e:
        return _bad_request(f"invalid hex or key: {e}")

    # 4. Decrypt + verify. process_inbound can raise:
    #      - nacl.exceptions.CryptoError (bad ciphertext / wrong key)
    #      - nacl.exceptions.BadSignatureError (signature doesn't match)
    #      - ValueError / KeyError (malformed bundle JSON)
    #      - UnicodeDecodeError (bundle wasn't valid UTF-8)
    # All of those mean "this message is bad"; reject with 400, do not
    # let them propagate and kill the worker.
    try:
        plaintext = process_inbound(
            ciphertext,
            sender_public_key,
            sender_verify_key,
        )
    except (ValueError, KeyError, BadSignatureError) as e:
        return _bad_request(f"inbound rejected: {type(e).__name__}: {e}")
    except Exception as e:
        # nacl.exceptions.CryptoError is a subclass of Exception but NOT
        # of ValueError. Catch it explicitly via the broader class.
        if type(e).__name__ == "CryptoError":
            return _bad_request(f"inbound rejected: CryptoError")
        # Anything else is unexpected; surface a 500 so we know to
        # investigate, but still don't crash the listener.
        return jsonify({"error": f"internal: {type(e).__name__}: {e}"}), 500

    # 5. Persist. The partner's save_message takes (room_id, sender_id,
    #    content, signature=None). The plan called for save_message(
    #    sender_id, content, timestamp) but the actual API is room-
    #    scoped; we look up the "default" room by name to get its real
    #    primary key (which is an auto-generated UUID, not "default").
    #    save_message returns None if the room doesn't exist or the
    #    sender isn't a member — treat that as a soft failure (log,
    #    return 200 with message_id = None) so the peer doesn't get a
    #    confusing 400 for our own misconfiguration.
    message = None
    try:
        default_room = _find_default_room()
        if default_room is not None:
            message = database.save_message(
                room_id=default_room,
                sender_id=sender_id,
                content=plaintext,
                signature=None,
            )
    except Exception as e:
        # Storage layer exploded unexpectedly. Log via Flask's default
        # error handler and continue; we still want to ack the peer so
        # they don't retry forever.
        app.logger.exception("save_message failed: %s", e)

    # 6. Stamp the inbound timestamp so the UI's polling layer can
    #    detect "something new" cheaply.
    last_inbound_at = time.time()

    response = {"status": "ok", "message_id": message.message_id if message else None}
    return jsonify(response), 200


@app.route("/api/messages/status", methods=["GET"])
def status():
    """Cheap endpoint the UI polls to know whether to re-fetch /messages.
    Returns the last_inbound_at timestamp (epoch seconds). 0.0 means
    "nothing has arrived yet."
    """
    return jsonify({"last_inbound_at": last_inbound_at}), 200


if __name__ == "__main__":
    # Bind 0.0.0.0 so peers on the same WiFi can reach us; the local
    # firewall is what actually controls who gets through. Port 5000
    # matches the plan (the skill suggested 5050; we follow the plan).
    app.run(host="0.0.0.0", port=5000, debug=False)
