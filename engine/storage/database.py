import hashlib
import hmac
import secrets
import time
from enum import Enum, auto
from typing import Any, Optional

from peewee import IntegrityError

from engine.storage.connection import get_db
from engine.storage.models import Message, Peer, Room, RoomMember


class JoinResult(Enum):
    """Possible outcomes of join_room(). Callers use this instead of
    inspecting True/False/None, which is ambiguous.
    """
    JOINED = auto()            # membership created successfully
    ALREADY_MEMBER = auto()    # peer was already in the room (idempotent)
    WRONG_PASSWORD = auto()    # private room + bad/missing password
    ROOM_NOT_FOUND = auto()    # no room with that id


class Role(Enum):
    """Membership role in a room."""
    ADMIN = "admin"
    MEMBER = "member"


# Database config methods

def init_db() -> None:
    """Initialize the database and create tables if they don't exist."""
    db = get_db()
    db.connect()
    db.create_tables([Peer, Message, Room, RoomMember], safe=True)


def close_db() -> None:
    """Close the database connection if it is currently open."""
    db = get_db()
    if not db.is_closed():
        db.close()


def reset_db(confirm: bool = False) -> None:
    """Drop and recreate all tables, wiping all stored data.

    Destructive. Pass `confirm=True` to acknowledge — prevents accidental
    data loss from a stray call.
    """
    if not confirm:
        raise RuntimeError(
            "reset_db() destroys all data. Pass confirm=True to proceed."
        )
    db = get_db()
    db.drop_tables([Peer, Message, Room, RoomMember], safe=True)
    db.create_tables([Peer, Message, Room, RoomMember], safe=True)


# Peer database operations

def _coerce(value: Any, default: Any, caster) -> Any:
    """Return cast(value) when value is not None, else default."""
    return caster(value) if value is not None else default


def save_peer(peer_data: dict) -> Peer:
    """Insert a new peer or update an existing one (upsert pattern).

    `None` values in `peer_data` mean "leave the existing field alone."
    """

    peer_id = peer_data["peer_id"]
    db = get_db()

    # Note: SqliteQueueDatabase doesn't support db.atomic(); we still
    # handle the get-or-create race via the IntegrityError catch below.
    peer = Peer.get_or_none(Peer.peer_id == peer_id)

    if peer is None:
        try:
            peer = Peer.create(
                peer_id=peer_id,
                public_key=peer_data.get("public_key") or "",
                name=peer_data.get("name") or "",
                ip_address=peer_data.get("ip_address") or "127.0.0.1",
                port=_coerce(peer_data.get("port"), 5000, int),
                last_active=_coerce(peer_data.get("last_active"), time.time(), float),
                is_online=_coerce(peer_data.get("is_online"), True, bool),
            )
        except IntegrityError:
            # Lost the race: another writer created the same peer_id
            # between our get_or_none and create. Fall through and
            # update the now-existing row.
            peer = Peer.get(Peer.peer_id == peer_id)

    # Update path: runs for the existing-peer case and for the
    # race-loss case above. None values mean "leave the existing
    # field alone".
    if peer_data.get("public_key") is not None:
        peer.public_key = _coerce(peer_data["public_key"], peer.public_key, str)
    if peer_data.get("name") is not None:
        peer.name = _coerce(peer_data["name"], peer.name, str)
    if peer_data.get("ip_address") is not None:
        peer.ip_address = _coerce(peer_data["ip_address"], peer.ip_address, str)
    if peer_data.get("port") is not None:
        peer.port = _coerce(peer_data["port"], peer.port, int)
    if peer_data.get("last_active") is not None:
        peer.last_active = _coerce(peer_data["last_active"], peer.last_active, float)
    if peer_data.get("is_online") is not None:
        peer.is_online = _coerce(peer_data["is_online"], peer.is_online, bool)

    peer.save()
    return peer


def get_peer(peer_id: str) -> Optional[Peer]:
    """Return peer by ID, or None if not found."""
    try:
        return Peer.get(Peer.peer_id == peer_id)
    except Peer.DoesNotExist:
        return None


def list_peers(online_only: bool = False) -> list[Peer]:
    """Return all peers, or only online peers if `online_only=True`."""
    query = Peer.select()
    if online_only:
        query = query.where(Peer.is_online == True)
    return list(query)


def mark_peer_online(peer_id: str) -> bool:
    """When a peer becomes online, mark is_online as True. Returns success."""
    try:
        peer = Peer.get(Peer.peer_id == peer_id)
        peer.is_online = True
        peer.last_active = time.time()
        peer.save()
        return True
    except Peer.DoesNotExist:
        return False


def mark_peer_offline(peer_id: str) -> bool:
    """When a peer becomes offline, mark is_online as False. Returns success."""
    try:
        peer = Peer.get(Peer.peer_id == peer_id)
        peer.is_online = False
        peer.save()
        return True
    except Peer.DoesNotExist:
        return False


def delete_peer(peer_id: str) -> bool:
    """Delete a peer by ID. Returns True if a row was removed, False otherwise."""
    deleted_count = (
        Peer.delete()
        .where(Peer.peer_id == peer_id)
        .execute()
    )
    return deleted_count > 0


# Room database operations

def create_room(
    creator_id: str,
    name: str,
    is_public: bool = True,
    password_hash: Optional[str] = None,
) -> Optional[Room]:
    """Create a new room and add the creator as its first admin member.

    Returns the saved Room instance, or None if the room could not be
    created (invalid input or unknown creator). `password_hash` must
    already be hashed by the caller.
    """
    if not name or not name.strip():
        return None
    if not creator_id or not creator_id.strip():
        return None
    if get_peer(creator_id) is None:
        return None

    # SqliteQueueDatabase doesn't support db.atomic() — but its writer
    # thread already serializes writes, so the two creates here can't
    # interleave with anything else.
    room = Room.create(
        creator_id=creator_id,
        is_public=is_public,
        name=name,
        password_hash=password_hash,
    )
    RoomMember.create(
        room=room,
        peer_id=creator_id,
        role=Role.ADMIN.value,
    )

    return room


def get_room(room_id: str) -> Optional[Room]:
    """Return room by ID, or None if not found."""
    try:
        return Room.get(Room.room_id == room_id)
    except Room.DoesNotExist:
        return None


def list_public_rooms() -> list[Room]:
    """Return all public rooms."""
    return list(Room.select().where(Room.is_public == True))


def list_rooms_by_creator(creator_id: str) -> list[Room]:
    """Return all rooms created by the given peer."""
    return list(Room.select().where(Room.creator_id == creator_id))


def delete_room(room_id: str, creator_id: str) -> bool:
    """Delete a room, but only if the caller is its creator.

    Returns True if a row was removed, False if no matching room existed
    or the caller wasn't the creator (we treat both as "no change").
    """
    deleted_count = (
        Room.delete()
        .where(
            (Room.room_id == room_id) & (Room.creator_id == creator_id)
        )
        .execute()
    )
    return deleted_count > 0


# Room membership operations

def join_room(room_id: str, peer_id: str, password: Optional[str] = None) -> JoinResult:
    """Add a peer to a room. Returns a JoinResult indicating the outcome.

    Outcomes:
        JoinResult.JOINED          - membership was created
        JoinResult.ALREADY_MEMBER  - peer was already a member (no-op)
        JoinResult.WRONG_PASSWORD  - private room, password didn't match
        JoinResult.ROOM_NOT_FOUND  - no room exists with that id
    """
    # Step 1: fetch the room. get_or_none returns the row or None.
    room = Room.get_or_none(Room.room_id == room_id)
    if room is None:
        return JoinResult.ROOM_NOT_FOUND

    # Step 2: decide whether the join is allowed.
    if room.is_public:
        can_join = True
    else:
        # Defensive: if the caller forgot the password on a private
        # room, don't crash inside verify_password trying to encode None.
        if password is None:
            return JoinResult.WRONG_PASSWORD
        can_join = verify_password(password, room.password_hash)

    if not can_join:
        return JoinResult.WRONG_PASSWORD

    # Step 3: create the membership.
    try:
        RoomMember.create(
            room=room,
            peer_id=peer_id,
            role=Role.MEMBER.value,
        )
    except IntegrityError:
        return JoinResult.ALREADY_MEMBER

    return JoinResult.JOINED


def leave_room(room_id: str, peer_id: str) -> bool:
    """Remove a peer from a room.

    If the leaving peer is the only admin of the room, the entire room
    is deleted (cascading removes all members and messages).
    Returns True if anything was changed, False if the peer wasn't a member.
    """
    # Find this peer's membership in the room.
    member = RoomMember.get_or_none(
        RoomMember.room_id == room_id,
        RoomMember.peer_id == peer_id,
    )
    if member is None:
        return False

    # If this peer is an admin and the only admin in this room, delete the room.
    if member.role == Role.ADMIN.value:
        admin_count = (
            RoomMember.select()
            .where(
                RoomMember.room_id == room_id,
                RoomMember.role == Role.ADMIN.value,
            )
            .count()
        )
        if admin_count == 1:
            delete_room(room_id, member.peer_id)
            return True

    # Normal leave: just remove the RoomMember row.
    deleted_count = (
        RoomMember.delete()
        .where(
            RoomMember.room_id == room_id,
            RoomMember.peer_id == peer_id,
        )
        .execute()
    )
    return deleted_count > 0


def is_room_member(room_id: str, peer_id: str) -> bool:
    """Return True if the peer is a member of the room, False otherwise."""
    return (
        RoomMember.select()
        .where(
            RoomMember.room_id == room_id,
            RoomMember.peer_id == peer_id,
        )
        .exists()
    )


def list_room_members(room_id: str) -> list[RoomMember]:
    """Return all members of a room, oldest-joined first."""
    return list(
        RoomMember.select()
        .where(RoomMember.room_id == room_id)
        .order_by(RoomMember.joined_at.asc())
    )


def list_joined_rooms(peer_id: str) -> list[Room]:
    """Return all rooms the peer is a member of, most recently joined first."""
    return list(
        Room.select()
        .join(RoomMember)
        .where(RoomMember.peer_id == peer_id)
        .order_by(RoomMember.joined_at.desc())
    )


# Password helpers for private rooms

PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-SHA256
PBKDF2_SALT_BYTES = 16
PBKDF2_KEY_BYTES = 32

# Reject stored cost parameters lower than this. Protects against
# tampering that would downgrade verification to a cheap hash.
PBKDF2_MIN_ITERATIONS = 100_000

# Cap iterations when reading from a stored hash to prevent CPU-DoS
# from a tampered DB row advertising a billion-round hash.
PBKDF2_MAX_ITERATIONS = 10_000_000


def _password_to_bytes(password: str | bytes) -> bytes:
    """Normalize a password to bytes. Accepts str or bytes."""
    if isinstance(password, bytes):
        return password
    return password.encode("utf-8")


def hash_password(password: str | bytes) -> str:
    """Hash a plaintext password into a self-contained string.

    The returned string format is:
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    so verify_password can recover salt and parameters from the stored
    value alone — no separate salt column needed.
    """
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    derived_key = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        _password_to_bytes(password),
        salt,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_KEY_BYTES,
    )
    return f"pbkdf2_{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${derived_key.hex()}"


def verify_password(password: str | bytes, password_hash: Optional[str]) -> bool:
    """Verify a plaintext password against a stored hash.

    Returns True if it matches, False otherwise. Never raises —
    any malformed input is treated as a non-match.
    """
    # Step 1: parse the stored string. If anything looks wrong, return False.
    if not password_hash:
        return False
    try:
        algorithm_tag, iterations_str, salt_hex, hash_hex = password_hash.split("$")
    except (ValueError, AttributeError):
        return False

    if algorithm_tag != f"pbkdf2_{PBKDF2_ALGORITHM}":
        return False

    try:
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    # Reject suspicious cost parameters (defends against DB tampering
    # and CPU-DoS via a forged hash with absurd iteration counts).
    if not (PBKDF2_MIN_ITERATIONS <= iterations <= PBKDF2_MAX_ITERATIONS):
        return False

    # Step 2: recompute the hash from the plaintext + recovered salt + cost.
    derived_key = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        _password_to_bytes(password),
        salt,
        iterations,
        dklen=len(expected_hash),
    )

    # Step 3: compare in constant time.
    return hmac.compare_digest(derived_key, expected_hash)


# Message database operations

MAX_MESSAGE_CONTENT_BYTES = 64 * 1024  # 64 KiB


def save_message(
    room_id: str,
    sender_id: str,
    content: str,
    signature: Optional[str] = None,
) -> Optional[Message]:
    """Save a new message to a room.

    The sender must be a member of the room. Returns the saved Message
    instance, or None if the room doesn't exist, the sender isn't a
    member, or the content is empty/too large.
    """
    if not content:
        return None
    if len(content.encode("utf-8")) > MAX_MESSAGE_CONTENT_BYTES:
        return None

    # SqliteQueueDatabase doesn't support db.atomic() — but its writer
    # thread serializes writes, so the membership read and the insert
    # can't be interleaved with a concurrent leave.
    room = Room.get_or_none(Room.room_id == room_id)
    if room is None:
        return None
    if not is_room_member(room_id, sender_id):
        return None
    message = Message.create(
        room=room,
        sender_id=sender_id,
        content=content,
        signature=signature,
    )
    return message


def get_message(message_id: str) -> Optional[Message]:
    """Fetch a message by ID, or None if it doesn't exist."""
    return Message.get_or_none(Message.message_id == message_id)


def list_messages(room_id: str, peer_id: str, limit: Optional[int] = None) -> list[Message]:
    """Return messages from a room, oldest first.

    The peer must be a member of the room — non-members get an empty list.
    Optional `limit` caps the number of returned messages (e.g., for
    paginating "load the last 50 messages").
    """
    if not is_room_member(room_id, peer_id):
        return []

    query = (
        Message.select()
        .where(Message.room_id == room_id)
        .order_by(Message.timestamp.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return list(query)


def list_unsynced_messages() -> list[Message]:
    """Return all messages where is_synced is False, oldest first.

    Used by the Matrix bridge worker to know which messages still need
    to be pushed upstream.
    """
    return list(
        Message.select()
        .where(Message.is_synced == False)
        .order_by(Message.timestamp.asc())
    )


def mark_message_synced(message_id: str, matrix_event_id: Optional[str] = None) -> bool:
    """Mark a message as synced to Matrix and record the Matrix event ID.

    Returns True if the message existed and was updated, False otherwise.
    Uses a single UPDATE query — no need to fetch the row first.
    """
    updated = (
        Message.update(
            is_synced=True,
            matrix_event_id=matrix_event_id,
        )
        .where(Message.message_id == message_id)
        .execute()
    )
    return updated > 0


def delete_message(message_id: str) -> bool:
    """Delete a message by ID. Returns True if a row was removed."""
    deleted_count = (
        Message.delete()
        .where(Message.message_id == message_id)
        .execute()
    )
    return deleted_count > 0