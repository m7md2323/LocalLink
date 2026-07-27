import time
import uuid

from peewee import (
    BooleanField,
    CharField,
    CompositeKey,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)

from engine.storage.database import db


class BaseModel(Model):
    """Base Peewee model bound to the LocalLink SQLite database."""

    class Meta:
        database = db


class Peer(BaseModel):
    """Represents a discovered node/peer device on the local mesh network."""

    peer_id = CharField(primary_key=True)
    public_key = TextField(default="")
    name = CharField(default="")
    ip_address = CharField(default="127.0.0.1")
    port = IntegerField(default=5000)
    last_active = FloatField(default=time.time)
    is_online = BooleanField(default=True)

    def endpoint_url(self) -> str:
        return f"http://{self.ip_address}:{self.port}"

    def mark_online(self) -> None:
        self.is_online = True
        self.last_active = time.time()

    def mark_offline(self) -> None:
        self.is_online = False

    def to_dict(self) -> dict:
        """Convert Peer to a dictionary for JSON transmission over P2P network."""
        return {
            "peer_id": self.peer_id,
            "public_key": self.public_key,
            "name": self.name,
            "ip_address": self.ip_address,
            "port": self.port,
            "last_active": self.last_active,
            "is_online": self.is_online,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Peer":
        """Construct a Peer instance from a received JSON dictionary payload."""
        return cls(
            peer_id=data["peer_id"],
            public_key=data.get("public_key", ""),
            name=data.get("name", ""),
            ip_address=data.get("ip_address", "127.0.0.1"),
            port=int(data.get("port", 5000)),
            last_active=float(data.get("last_active", time.time())),
            is_online=bool(data.get("is_online", True)),
        )

class Message(BaseModel):
    """Represents a message payload sent over the local mesh network or Matrix bridge."""

    message_id = CharField(primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id = CharField()
    content = TextField()
    room_id = CharField(default="default")
    signature = TextField(null=True)
    timestamp = FloatField(default=time.time)
    is_synced = BooleanField(default=False)
    matrix_event_id = CharField(null=True)

    def mark_synced(self) -> None:
        self.is_synced = True

    def to_dict(self) -> dict:
        """Convert Message to a dictionary for JSON transmission over P2P network."""
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "content": self.content,
            "room_id": self.room_id,
            "signature": self.signature,
            "timestamp": self.timestamp,
            "is_synced": self.is_synced,
            "matrix_event_id": self.matrix_event_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Construct a Message instance from a received JSON dictionary payload."""
        return cls(
            message_id=data.get("message_id", str(uuid.uuid4())),
            sender_id=data["sender_id"],
            content=data["content"],
            room_id=data.get("room_id", "default"),
            signature=data.get("signature"),
            timestamp=float(data.get("timestamp", time.time())),
            is_synced=bool(data.get("is_synced", False)),
            matrix_event_id=data.get("matrix_event_id"),
        )


class Room(BaseModel):
    """Represents a messaging room or channel between peers."""

    room_id = CharField(primary_key=True, default=lambda: str(uuid.uuid4()))
    name = CharField(default="")
    creator_id = CharField()
    is_public = BooleanField(default=True)
    password_hash = CharField(null=True)
    created_at = FloatField(default=time.time)
    matrix_room_id = CharField(null=True)

    def to_dict(self) -> dict:
        """Convert Room to a dictionary for JSON transmission over P2P network."""
        return {
            "room_id": self.room_id,
            "name": self.name,
            "creator_id": self.creator_id,
            "is_public": self.is_public,
            "created_at": self.created_at,
            "matrix_room_id": self.matrix_room_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Room":
        """Construct a Room instance from a received JSON dictionary payload."""
        return cls(
            room_id=data.get("room_id", str(uuid.uuid4())),
            name=data.get("name", ""),
            creator_id=data["creator_id"],
            is_public=bool(data.get("is_public", True)),
            password_hash=data.get("password_hash"),
            created_at=float(data.get("created_at", time.time())),
            matrix_room_id=data.get("matrix_room_id"),
        )


class RoomMember(BaseModel):
    """Represents a peer's membership in a room."""

    room = ForeignKeyField(Room, backref="members", on_delete="CASCADE")
    peer_id = CharField()
    joined_at = FloatField(default=time.time)
    role = CharField(default="member")

    class Meta:
        primary_key = CompositeKey("room", "peer_id")

    def to_dict(self) -> dict:
        """Convert RoomMember to a dictionary for JSON transmission."""
        return {
            "room_id": self.room_id,
            "peer_id": self.peer_id,
            "joined_at": self.joined_at,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoomMember":
        """Construct a RoomMember instance from a received JSON dictionary payload."""
        return cls(
            room=data["room_id"],
            peer_id=data["peer_id"],
            joined_at=float(data.get("joined_at", time.time())),
            role=data.get("role", "member"),
        )

