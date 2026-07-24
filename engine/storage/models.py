import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Peer:
    """Represents a discovered node/peer device on the local mesh network."""
    peer_id: str
    public_key: str = ""
    name: str = ""
    ip_address: str = "127.0.0.1"
    port: int = 5000
    last_active: float = field(default_factory=time.time)
    is_online: bool = True

    def endpoint_url(self) -> str:
        return f"http://{self.ip_address}:{self.port}"

    def mark_online(self):
        self.is_online = True
        self.last_active = time.time()

    def mark_offline(self):
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

    def to_db_tuple(self) -> tuple:
        """Convert Peer to an ordered tuple for SQLite INSERT queries."""
        return (
            self.peer_id,
            self.public_key,
            self.name,
            self.ip_address,
            self.port,
            self.last_active,
            1 if self.is_online else 0,
        )

    @classmethod
    def from_db_row(cls, row: tuple) -> "Peer":
        """Construct a Peer instance from an SQLite query result row."""
        return cls(
            peer_id=row[0],
            public_key=row[1],
            name=row[2],
            ip_address=row[3],
            port=row[4],
            last_active=float(row[5]),
            is_online=bool(row[6]),
        )


@dataclass
class Message:
    """Represents a message payload sent over the local mesh network or Matrix bridge."""
    sender_id: str
    content: str
    room_id: str = "default"
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signature: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    is_synced: bool = False
    matrix_event_id: Optional[str] = None

    def mark_synced(self) :
        self.is_synced=True

    def to_dict(self) -> dict:
        """Convert Message to a dictionary for JSON transmission over P2P network."""
        return {
            "sender_id": self.sender_id,
            "content": self.content,
            "room_id": self.room_id,
            "message_id": self.message_id,
            "signature": self.signature,
            "timestamp": self.timestamp,
            "is_synced": self.is_synced,
            "matrix_event_id": self.matrix_event_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Construct a Message instance from a received JSON dictionary payload."""
        return cls(
            sender_id=data["sender_id"],
            content=data["content"],
            room_id=data.get("room_id", "default"),
            message_id=data.get("message_id", str(uuid.uuid4())),
            signature=data.get("signature"),
            timestamp=float(data.get("timestamp", time.time())),
            is_synced=bool(data.get("is_synced", False)),
            matrix_event_id=data.get("matrix_event_id"),
        )

    def to_db_tuple(self) -> tuple:
        """Convert Message to an ordered tuple for SQLite INSERT queries."""
        return (
            self.message_id,
            self.sender_id,
            self.room_id,
            self.content,
            self.signature,
            self.timestamp,
            1 if self.is_synced else 0,
            self.matrix_event_id,
        )

    @classmethod
    def from_db_row(cls, row: tuple) -> "Message":
        """Construct a Message instance from an SQLite query result row."""
        return cls(
            message_id=row[0],
            sender_id=row[1],
            room_id=row[2],
            content=row[3],
            signature=row[4],
            timestamp=float(row[5]),
            is_synced=bool(row[6]),
            matrix_event_id=row[7],
        )


@dataclass
class Room:
    """Represents a messaging room or channel between peers."""
    room_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    is_direct: bool = True
    created_at: float = field(default_factory=time.time)
    matrix_room_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert Room to a dictionary for JSON transmission over P2P network."""
        return {
            "room_id": self.room_id,
            "name": self.name,
            "is_direct": self.is_direct,
            "created_at": self.created_at,
            "matrix_room_id": self.matrix_room_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Room":
        """Construct a Room instance from a received JSON dictionary payload."""
        return cls(
            room_id=data.get("room_id", str(uuid.uuid4())),
            name=data.get("name", ""),
            is_direct=bool(data.get("is_direct", True)),
            created_at=float(data.get("created_at", time.time())),
            matrix_room_id=data.get("matrix_room_id"),
        )

    def to_db_tuple(self) -> tuple:
        """Convert Room to an ordered tuple for SQLite INSERT queries."""
        return (
            self.room_id,
            self.name,
            1 if self.is_direct else 0,
            self.created_at,
            self.matrix_room_id,
        )

    @classmethod
    def from_db_row(cls, row: tuple) -> "Room":
        """Construct a Room instance from an SQLite query result row."""
        return cls(
            room_id=row[0],
            name=row[1],
            is_direct=bool(row[2]),
            created_at=float(row[3]),
            matrix_room_id=row[4],
        )