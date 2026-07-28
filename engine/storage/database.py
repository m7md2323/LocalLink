import hashlib
import hmac
import os
import secrets
import time
from enum import Enum, auto

from peewee import IntegrityError, SqliteQueueDatabase
from engine.storage.models import Peer, Message, Room, RoomMember


class JoinResult(Enum):
    """Possible outcomes of join_room(). Callers use this instead of
    inspecting True/False/None, which is ambiguous.
    """
    JOINED = auto()            # membership created successfully
    ALREADY_MEMBER = auto()    # peer was already in the room (idempotent)
    WRONG_PASSWORD = auto()    # private room + bad/missing password
    ROOM_NOT_FOUND = auto()    # no room with that id
    
    
# Load DB path from environment or default to locallink.db
db_path = os.getenv("LOCALLINK_DB_PATH", "locallink.db")
db = SqliteQueueDatabase(db_path, pragmas={
    'journal_mode': 'wal',  # Allow readers while writer active.
    'cache_size': -64000,  # 64 MB page cache.
    'foreign_keys': 1,  # Enforce FK constraints.
},
    autostart=True
)

# Note: All of database methods are shrunk here, later we can make each model with his own methods in a seperated file.

# Database config methods

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    db.connect()
    db.create_tables([Peer, Message, Room, RoomMember], safe=True)


def close_db():
    """Close the database connection if it is currently open."""
    if not db.is_closed():
        db.close()


def reset_db():
    """Drop and recreate all tables, wiping all stored data."""
    db.drop_tables([Peer, Message, Room, RoomMember], safe=True)
    db.create_tables([Peer, Message, Room, RoomMember], safe=True)


# Peer database operations

def save_peer(peer_data):
    """Insert a new peer or update an existing one (upsert pattern)."""

    peer_id = peer_data["peer_id"]

    try:
        # Try to get peer if already stored.
        peer = Peer.get(Peer.peer_id == peer_id)

        # If we are here, then no exception is raised, we update the peer.
        peer.public_key = peer_data.get("public_key", peer.public_key)
        peer.name = peer_data.get("name", peer.name)
        peer.ip_address = peer_data.get("ip_address", peer.ip_address)
        peer.port = int(peer_data.get("port", peer.port))
        peer.last_active = float(peer_data.get("last_active", time.time()))
        peer.is_online = bool(peer_data.get("is_online", True))

        # Must run .save() to store the update.
        peer.save()

    except Peer.DoesNotExist:
        # If peer does not exist, ceate one.
        peer = Peer.create(
            peer_id=peer_id,
            public_key=peer_data.get("public_key", ""),
            name=peer_data.get("name", ""),
            ip_address=peer_data.get("ip_address", "127.0.0.1"),
            port=int(peer_data.get("port", 5000)),
            last_active=float(peer_data.get("last_active", time.time())),
            is_online=bool(peer_data.get("is_online", True)),
        )

    # Return the saved peer .
    return peer


def get_peer(peer_id):
    """Return peer by ID"""
    try:
        return Peer.get(Peer.peer_id == peer_id)
    except Peer.DoesNotExist:
        return None
    


def list_peers(online_only=False):
    """Return all or only online peers"""
    peers=None
    if not online_only :
        peers=Peer.select()   
    else :
        peers=Peer.select().where(Peer.is_online==True)    
    return list(peers)


def mark_peer_online(peer_id):
    """When a peer becomes online, mark is_online as True."""
    try:
        peer = Peer.get(Peer.peer_id == peer_id)
        peer.is_online=True
        peer.last_active=time.time()
        peer.save()
        return True
    except Peer.DoesNotExist:
        return False
        

def mark_peer_offline(peer_id):
    """When a peer becomes offline, mark is_online as False."""
    try:
        peer = Peer.get(Peer.peer_id == peer_id)
        peer.is_online=False
        peer.save()
        return True
    except Peer.DoesNotExist:
        return False
        

def delete_peer(peer_id):
    """Delete a peer by ID. Returns True if a row was removed, False otherwise."""

    deleted_count = (
        Peer.delete()
        .where(Peer.peer_id == peer_id)
        .execute()
    )

    return deleted_count > 0


# Room database operations

def create_room(creator_id, name, is_public=True, password_hash=None):
    """Create a new room and add the creator as its first admin member.

    Returns the saved Room instance, or None if the room could not be created.
    Note: password_hash must already be hashed by the caller.
    """
    
    with db.atomic():
        # Step 1: insert the Room row. This returns the saved instance
        room = Room.create(
            creator_id=creator_id,
            is_public=is_public,
            name=name,
            password_hash=password_hash,
        )

        # Step 2: add the creator as the first member.
        RoomMember.create(
            room=room,
            peer_id=creator_id,
            role="admin",
        )

    return room
    


def get_room(room_id):
    """Return room by ID"""
    try:
        return Room.get(Room.room_id==room_id)
    except Room.DoesNotExist:
        return None



def list_public_rooms():
    """Return all public rooms"""
    rooms=Room.select().where(Room.is_public==True)
    return list(rooms)


def list_rooms_by_creator(creator_id):
    rooms=Room.select().where(Room.creator_id == creator_id)
    return list(rooms)


def delete_room(room_id, creator_id):
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

def join_room(room_id, peer_id, password=None):
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
            role="member",
        )
    except IntegrityError:
        return JoinResult.ALREADY_MEMBER

    return JoinResult.JOINED


def leave_room(room_id, peer_id):
    """Remove a peer from a room.

    If the leaving peer is the only admin of the room, the entire room
    is deleted (cascading removes all members and messages).
    Returns True if anything was changed, False if the peer wasn't a member.
    """
    # Find this peer's membership in the room. 
    member = RoomMember.get_or_none(
        RoomMember.room.room_id == room_id,
        RoomMember.peer_id == peer_id,
    )
    # If the peer is not a member, return false.
    if member is None:
        return False

    # If this peer is an admin and the only admin in this room, delete the room. 
    if member.role == "admin":
        admin_count = (
            RoomMember.select()
            .where(
                RoomMember.room.room_id == room_id,
                RoomMember.role == "admin",
            )
            .count()
        )
        if admin_count == 1:

            delete_room(room_id)
            return True

    # This is a normal leave, just remove the RoomMember row.
    deleted_count = (
        RoomMember.delete()
        .where(
            RoomMember.room.room_id == room_id,
            RoomMember.peer_id == peer_id,
        )
        .execute()
    )
    return deleted_count > 0


def is_room_member(room_id, peer_id):
    """Return True if the peer is a member of the room, False otherwise."""
    
    return (
        RoomMember.select()
        .where(
            RoomMember.room.room_id == room_id,
            RoomMember.peer_id == peer_id,
        )
        .exists()
    )


def list_room_members(room_id):
    """Return all members of a room, oldest-joined first."""
    
    return list(
        RoomMember.select()
        .where(RoomMember.room.room_id == room_id)
        .order_by(RoomMember.joined_at.asc())
    )


def list_joined_rooms(peer_id):
    """Return all rooms the peer is a member of, most recently joined first."""
    
    return list(
        Room.select()
        .join(RoomMember)
        .where(RoomMember.peer_id == peer_id)
        .order_by(RoomMember.joined_at.desc())
    )


# Password helpers for private rooms

PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 200_000  # ~hundreds of ms on modern hardware
PBKDF2_SALT_BYTES = 16
PBKDF2_KEY_BYTES = 32


def hash_password(password):
    """Hash a plaintext password into a self-contained string.

    The returned string format is:
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    so verify_password can recover salt and parameters from the stored
    value alone — no separate salt column needed.
    """
    # Step 1: generate a fresh random salt.
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)

    # Step 2: derive a key from the password + salt.
    derived_key = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_KEY_BYTES,
    )

    # Step 3: bundle everything into one string for storage.
    return f"pbkdf2_{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${derived_key.hex()}"


def verify_password(password, password_hash):
    """Verify a plaintext password against a stored hash.

    Returns True if it matches, False otherwise. Never raises —
    any malformed input is treated as a non-match.
    """
    # Step 1: parse the stored string. If anything looks wrong, return False.
    try:
        algorithm_tag, iterations_str, salt_hex, hash_hex = password_hash.split("$")
    except ValueError:
        return False

    if algorithm_tag != f"pbkdf2_{PBKDF2_ALGORITHM}":
        return False

    try:
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
        
    except ValueError:
        return False

    # Step 2: recompute the hash from the plaintext + recovered salt + cost.
    derived_key = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected_hash),
    )

    # Step 3: compare in CONSTANT TIME.
    return hmac.compare_digest(derived_key, expected_hash)


# Message database operations

def save_message(room_id, sender_id, content, signature=None):
    """Save a new message to a room.

    The sender must be a member of the room. Returns the saved Message
    instance, or None if the room doesn't exist or the sender isn't a member.
    """
    # Wrap the membership check and the insert in one transaction so a
    # concurrent leave can't slip a non-member's message through between
    # the check and the insert.
    with db.atomic():
        room = Room.get_or_none(Room.room_id == room_id)
        if room is None:
            return None
        if not is_room_member(room_id, sender_id):
            return None
        message = Message.create(
            room_id=room_id,
            sender_id=sender_id,
            content=content,
            signature=signature,
        )
    return message


def get_message(message_id):
    """Fetch a message by ID, or None if it doesn't exist."""
    return Message.get_or_none(Message.message_id == message_id)


def list_messages(room_id, peer_id, limit=None):
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


def list_unsynced_messages():
    """Return all messages where is_synced is False, oldest first.

    Used by the Matrix bridge worker to know which messages still need
    to be pushed upstream.
    """
    return list(
        Message.select()
        .where(Message.is_synced == False)
        .order_by(Message.timestamp.asc())
    )


def mark_message_synced(message_id, matrix_event_id=None):
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


def delete_message(message_id):
    """Delete a message by ID. Returns True if a row was removed."""
    deleted_count = (
        Message.delete()
        .where(Message.message_id == message_id)
        .execute()
    )
    return deleted_count > 0


