import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Tuple

@dataclass
class Peer:
    """Represents the peer device."""
    peer_id: str
    name: str
    ip_address: str 
    port: int
    last_active: float
    is_online: bool

    
@dataclass
class Message:
    """Represents a message payload sent over the local mesh network or Matrix bridge."""
    sender_id: str
    content: str
    room_id: str = "default"
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    is_synced: bool = False

