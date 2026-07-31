"""
engine/mesh/server.py

Flask listener for inbound LocalLink messages. Runs on this device so
remote peers can POST encrypted, signed bundles to us, and we can decrypt,
verify authorship, and persist the message locally.

Multi-room routing (Phase 2):
  In the single-room design, every inbound message was dumped into
  the "default" room. Now the encrypted plaintext is expected to be a
  JSON envelope: ``{"room_id": "...", "content": "..."}``. The room
  is resolved by id; the sender is auto-joined if the room is public
  and they aren't already a member. Private rooms reject unknown
  senders with 403.

  For backwards compatibility, if the decrypted plaintext is NOT a
  valid envelope (i.e. legacy clients still sending bare text), we
  fall back to the "default" room lookup. New clients always send the
  envelope.

Routes:
  POST /api/messages/receive
    Body: {
      "sender_public_key":     "<hex, our peer's Box public key>",
      "sender_verify_key":     "<hex, our peer's Ed25519 verify key>",
      "sender_id":             "<peer_id string, e.g. UUID>",
      "payload":               "<hex, ciphertext from prepare_outbound>"
    }
    The encrypted payload, when decrypted, is a JSON envelope:
        {"room_id": "<uuid>", "content": "<utf-8 text>"}
    Responses:
      200 {"status": "ok", "message_id": "..."} on success
      400 {"error": "..."} on any failure (bad JSON, bad hex, bad sig,
          tampered ciphertext, malformed bundle, unknown room, etc.)
      403 {"error": "..."} sender is not a member of a private room
      404 {"error": "..."} the addressed room does not exist
      500 {"error": "..."} only on an unexpected internal error

  GET /api/rooms
    Returns the public rooms on this node:
        [{"room_id": "...", "name": "...", "creator_id": "...",
          "is_public": true}, ...]
    Used by newly-discovered peers to learn what rooms exist here so
    the user can join them.

  POST /api/rooms/join
    Body: {"room_id": "...", "sender_id": "...",
           "password": "..." | null}
    Response: {"result": "<JoinResult enum name>"} where result is one
    of "joined", "already_member", "wrong_password", "not_found".
    Used by the client's join_remote_room() helper.

  GET /api/messages/status
    Cheap endpoint the UI polls to know whether to re-fetch /messages.
    Returns the last_inbound_at timestamp (epoch seconds). 0.0 means
    "nothing has arrived yet."

Design choices:
  - One Flask app, no blueprints (per AGENTS.md). Routes are grouped
    by responsibility (messages vs rooms) but live in the same file.
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

    Used as the backwards-compat fallback for legacy clients that
    don't include ``room_id`` in their envelope.
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


def _parse_envelope(plaintext: str):
    """Extract ``(room_id, content)`` from the decrypted plaintext.

    Tries the new envelope format first:
        {"room_id": "<uuid>", "content": "<text>"}

    On failure, falls back to (None, plaintext) — the caller treats
    ``room_id=None`` as "use the default room" for backwards compat
    with legacy clients that send bare text.

    Returns (room_id_or_None, content_string).
    """
    try:
        env = json.loads(plaintext)
    except (ValueError, TypeError):
        return None, plaintext
    if not isinstance(env, dict):
        return None, plaintext
    room_id = env.get("room_id")
    content = env.get("content")
    if not isinstance(content, str):
        return None, plaintext
    return (room_id if isinstance(room_id, str) else None), content


def _ensure_member(room, peer_id: str) -> tuple[bool, int]:
    """Make ``peer_id`` a member of ``room`` if policy allows.

    Returns ``(allowed, http_status)``:
        (True, 200)   — member now (or already was).
        (False, 403)  — private room and peer isn't a member.
    """
    if database.is_room_member(room.room_id, peer_id):
        return True, 200
    if room.is_public:
        database.join_room(room.room_id, peer_id, password=None)
        return True, 200
    return False, 403


@app.route("/api/messages/receive", methods=["POST"])
def receive():
    """Receive one encrypted, signed message bundle from a peer.

    Failure mode contract: any of the following returns 400, never 500:
      - body is not valid JSON
      - any required field is missing
      - any hex field isn't valid hex
      - the ciphertext doesn't decrypt (wrong key, tampered, corrupted)
      - the signature doesn't verify (wrong sender key, payload tampered)
      - the decrypted envelope is malformed
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

    # 5. Parse the envelope to figure out the target room and content.
    #    Legacy clients (pre-room) send bare text; we treat those as
    #    destined for the "default" room.
    target_room_id, content = _parse_envelope(plaintext)
    if target_room_id is None:
        default_room = _find_default_room()
        if default_room is None:
            return _bad_request("no room_id in envelope and no 'default' room exists")
        target_room_id = default_room

    # 6. Resolve the room and enforce membership. Public rooms auto-join
    #    the sender on first contact; private rooms require explicit
    #    membership and reject otherwise with 403.
    room = database.get_room(target_room_id)
    if room is None:
        return jsonify({"error": f"room not found: {target_room_id}"}), 404

    allowed, status = _ensure_member(room, sender_id)
    if not allowed:
        return jsonify({"error": "not a member of private room"}), status

    # 7. Persist. save_message returns None if the room doesn't exist
    #    or the sender isn't a member — treat that as a soft failure
    #    (log, return 200 with message_id = None) so the peer doesn't
    #    get a confusing 400 for our own misconfiguration.
    message = None
    try:
        message = database.save_message(
            room_id=room.room_id,
            sender_id=sender_id,
            content=content,
            signature=None,
        )
    except Exception as e:
        # Storage layer exploded unexpectedly. Log via Flask's default
        # error handler and continue; we still want to ack the peer so
        # they don't retry forever.
        app.logger.exception("save_message failed: %s", e)

    # 8. Stamp the inbound timestamp so the UI's polling layer can
    #    detect "something new" cheaply.
    last_inbound_at = time.time()

    response = {"status": "ok", "message_id": message.message_id if message else None}
    return jsonify(response), 200


@app.route("/api/rooms", methods=["GET"])
def list_rooms():
    """List the public rooms on this node.

    Used by newly-discovered peers to populate their room list. Private
    rooms are NOT advertised here — to learn about a private room the
    sender has to be told its room_id out of band (e.g. via chat).
    """
    rooms = database.list_public_rooms()
    return jsonify([{
        "room_id": r.room_id,
        "name": r.name,
        "creator_id": r.creator_id,
        "is_public": r.is_public,
    } for r in rooms]), 200


@app.route("/api/rooms/join", methods=["POST"])
def join_room_route():
    """Add the requesting peer to a room.

    Body: ``{"room_id": "...", "sender_id": "...", "password": "..." | null}``

    Trust model: this endpoint is reachable from any peer on the mesh
    without authentication. For a hackathon LAN that's fine. For a
    production deployment, the request would need to be signed with
    the sender's SigningKey (same envelope as /api/messages/receive).
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return _bad_request("body is not valid JSON")
    if not isinstance(data, dict):
        return _bad_request("body must be a JSON object")

    room_id = data.get("room_id")
    sender_id = data.get("sender_id")
    password = data.get("password")
    if not room_id or not sender_id:
        return _bad_request("missing room_id or sender_id")

    result = database.join_room(room_id, sender_id, password)
    return jsonify({"result": result.name}), 200


@app.route("/api/rooms/<room_id>/messages", methods=["GET"])
def room_history(room_id):
    """Pull recent messages from a room, optionally since a timestamp.

    Query params:
        since: float, epoch seconds. Only messages with timestamp > since
               are returned. Defaults to 0.0 (everything).

    Used by a peer that just joined (or rejoined) a room to catch up on
    history it missed. The sender_id is required so non-members get
    nothing.
    """
    sender_id = request.args.get("sender_id")
    if not sender_id:
        return _bad_request("missing sender_id query param")
    if not database.is_room_member(room_id, sender_id):
        return jsonify({"error": "not a member"}), 403

    try:
        since = float(request.args.get("since", "0") or "0")
    except ValueError:
        return _bad_request("invalid since timestamp")

    messages = database.list_messages(room_id, sender_id)
    if since > 0:
        messages = [m for m in messages if m.timestamp > since]

    return jsonify({
        "messages": [{
            "message_id": m.message_id,
            "sender_id": m.sender_id,
            "content": m.content,
            "timestamp": m.timestamp,
        } for m in messages],
    }), 200


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
