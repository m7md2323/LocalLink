"""
engine/api.py

TUI-facing facade. The Textual UI in ``cli/main.py`` never imports
peewee, the Flask app, or the HTTP client directly — it only sees the
functions defined here.

Why a facade at all?
  - Single import surface for the UI: ``from engine import api``.
  - Decouples UI refresh logic from storage and network.
  - Lets us swap Textual for a web client later without rewriting the
    engine.

The split between "reads from local DB" and "talks to a remote peer"
mirrors the engine's actual layers: anything in ``local_*`` is just a
DB query; anything in ``remote_*`` hits the network.
"""

import logging
import time
from typing import Optional

from engine.network import client as net_client
from engine.storage import database
from engine.storage.models import Peer, Room, Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local reads — pure DB queries, safe to call from any thread.
# ---------------------------------------------------------------------------


def list_peers() -> list[Peer]:
    """All known peers (online + offline)."""
    return database.list_peers()


def list_online_peers() -> list[Peer]:
    """Only peers that mDNS currently thinks are online."""
    return database.list_peers(online_only=True)


def prune_stale_peers(max_age_seconds: float = 90.0) -> int:
    """Mark peers offline if they haven't been seen in a while.

    The TUI calls this on each refresh tick so the sidebar catches up
    with reality faster than mDNS's TTL-based remove events would
    allow. Returns the number of peers marked offline.
    """
    return database.prune_stale_peers(max_age_seconds=max_age_seconds)


def list_rooms_for(peer_id: str) -> list[Room]:
    """Rooms this peer is a member of, most recently joined first.

    Drives the left-hand room list in the TUI.
    """
    return database.list_joined_rooms(peer_id)


def list_messages(room_id: str, peer_id: str, limit: Optional[int] = None) -> list[Message]:
    """Messages in a room, oldest first. Peer must be a member or
    the underlying query returns an empty list (not an exception).
    """
    return database.list_messages(room_id, peer_id, limit=limit)


def get_self_peer() -> Optional[Peer]:
    """Look up the local self peer.

    Derives the peer_id the same way bootstrap does: from the env var
    LOCALLINK_NODE_ID if set, otherwise from the stable signing key on
    disk. This means it works correctly even when no .env is present
    (i.e. when running as a standalone .exe).
    """
    import os
    peer_id = os.environ.get("LOCALLINK_NODE_ID", "").strip()
    if not peer_id:
        try:
            import hashlib
            from engine.security.keys import load_or_create_signing_key
            signing_key = load_or_create_signing_key()
            key_hash = hashlib.sha256(bytes(signing_key)).hexdigest()[:12]
            peer_id = f"ll-{key_hash}"
        except Exception:
            return None
    return database.get_peer(peer_id)


def get_room(room_id: str) -> Optional[Room]:
    """Look up a room by id (the row the TUI selected)."""
    return database.get_room(room_id)


# ---------------------------------------------------------------------------
# Local writes — change the DB directly. The TUI is the only place
# that creates rooms on this node; the network layer never does.
# ---------------------------------------------------------------------------


def create_local_room(creator_id: str, name: str, is_public: bool, password: Optional[str]) -> Optional[Room]:
    """Create a room on this node and add the creator as admin.

    ``password`` is the plaintext; we hash it via the storage layer's
    PBKDF2 helper before persisting. Returns the Room, or None on
    invalid input (empty name, unknown creator).
    """
    password_hash = database.hash_password(password) if password else None
    return database.create_room(
        creator_id=creator_id,
        name=name,
        is_public=is_public,
        password_hash=password_hash,
    )


def leave_room(room_id: str, peer_id: str) -> bool:
    """Leave a room. If the leaving peer was the only admin, the room
    is deleted (and its messages cascade).
    """
    return database.leave_room(room_id, peer_id)


# ---------------------------------------------------------------------------
# Network — talk to remote peers via the HTTP layer.
# ---------------------------------------------------------------------------


def send_to_room(peer: Peer, room_id: str, text: str, local_peer_id: str) -> bool:
    """Send ``text`` to ``room_id`` on the peer's node.

    Wraps ``engine.network.client.send_to_room`` so the TUI doesn't
    need to know about cryptography, key loading, or the local-DB
    pre-save. Returns True on a 200 from the peer.
    """
    return net_client.send_to_room(peer, room_id, text, local_peer_id)


def save_outgoing(room_id: str, text: str, local_peer_id: str) -> Optional[Message]:
    """Persist an outgoing message locally WITHOUT any network send.

    This is the offline path: the row lands in the DB with
    ``is_synced=False``, so the Matrix bridge (or a future mesh
    redelivery pass) can pick it up later. Returns the saved Message,
    or None if the sender isn't a member of the room.
    """
    return net_client.save_outbound(room_id, text, local_peer_id)


def deliver_to_peer(peer: Peer, room_id: str, text: str, local_peer_id: str) -> bool:
    """Network-only send of an already-saved message to one peer.

    Pairs with ``save_outgoing``: save once, then fan out to every
    online peer via this function without duplicating the local row.
    Returns True on a 200 from the peer.
    """
    return net_client.send_bundle_to_peer(peer, room_id, text, local_peer_id)


def list_remote_rooms(peer: Peer) -> list[dict]:
    """Fetch the public rooms on ``peer`` via GET /api/rooms."""
    return net_client.list_remote_rooms(peer)


def join_remote_room(peer: Peer, room_id: str, local_peer_id: str, password: Optional[str] = None) -> str:
    """Ask ``peer`` to add us to ``room_id`` via POST /api/rooms/join.

    Returns the JoinResult name string (``"JOINED"``, etc.). Does NOT
    create a local copy of the room — the peer's response is just an
    admission ticket; the actual room is created locally as a
    read-only mirror via ``mirror_remote_room`` after a successful
    join.
    """
    return net_client.join_remote_room(peer, room_id, local_peer_id, password)


def pull_room_history(peer: Peer, room_id: str, local_peer_id: str) -> list[dict]:
    """Pull history from a remote peer's room.

    Returns the list of message dicts from the peer's
    /api/rooms/{id}/messages endpoint. The caller is responsible for
    persisting them locally with is_synced=True (since they were
    delivered) — this function does not write to the DB because
    different callers may want different storage policies.
    """
    return net_client.pull_room_history(peer, room_id, local_peer_id, since=0.0)


# ---------------------------------------------------------------------------
# Higher-level helpers — combine DB + network into a single TUI call.
# ---------------------------------------------------------------------------


def mirror_remote_room(peer: Peer, room_meta: dict) -> Optional[Room]:
    """Create a local Room that mirrors a remote peer's room.

    We can't share a room across nodes (the schema is local-first),
    so when a peer advertises a public room, we make a local copy
    keyed by the same ``room_id``. The remote peer's room_id IS our
    local room_id — that way messages addressed to it on either side
    land in the same row in each peer's local DB.

    Returns the local Room, or None if the input is malformed. If a
    local Room with this id already exists, returns it unchanged.
    """
    room_id = room_meta.get("room_id")
    name = room_meta.get("name")
    creator_id = room_meta.get("creator_id")
    is_public = bool(room_meta.get("is_public", True))
    if not room_id or not name or not creator_id:
        return None

    existing = database.get_room(room_id)
    if existing is not None:
        return existing

    # Disambiguate the local display name so two peers' identically-named
    # rooms (most commonly "default") don't collide in our sidebar. The
    # room_id is unchanged, so cross-node message routing still works —
    # only our local label differs. If the remote peer has no name yet,
    # fall back to the raw name.
    peer_label = (peer.name or "").strip()
    local_name = f"{peer_label}'s {name}" if peer_label else name

    # Direct Peewee create because the storage-layer create_room()
    # validates that the creator is a known local peer. For a remote
    # peer's room, the creator is the OTHER node — we have their peer
    # record (from mDNS) but the storage layer wouldn't accept it
    # without us re-asserting membership.
    try:
        return Room.create(
            room_id=room_id,
            name=local_name,
            creator_id=creator_id,
            is_public=is_public,
            password_hash=None,
        )
    except Exception as e:
        # Likely an integrity error because the room was created
        # concurrently. Re-fetch and return whatever's there now.
        logger.warning("mirror_remote_room: create failed (%s), refetching", e)
        return database.get_room(room_id)


def ensure_membership(room_id: str, peer_id: str) -> bool:
    """Make sure ``peer_id`` is a member of ``room_id`` locally.

    Used after mirroring a remote room, so messages we route to it
    don't get rejected by the storage layer's membership check.
    """
    if database.is_room_member(room_id, peer_id):
        return True
    try:
        result = database.join_room(room_id, peer_id, password=None)
        return result.name in ("JOINED", "ALREADY_MEMBER")
    except Exception as e:
        logger.warning("ensure_membership failed for room=%s peer=%s: %s",
                       room_id, peer_id, e)
        return False


def update_self_name(peer_id: str, new_name: str) -> bool:
    """Update the display name of the local peer in the database."""
    try:
        database.save_peer({"peer_id": peer_id, "name": new_name})
        return True
    except Exception as e:
        logger.warning("update_self_name failed: %s", e)
        return False
