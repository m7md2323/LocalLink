"""
run.py

LocalLink master entrypoint. Boots the full stack in the correct order:

  1. Initialize the SQLite database (create tables if missing).
  2. Load or generate this node's identity (signing + encryption keys)
     and its peer_id (from LOCALLINK_NODE_ID env var, or fresh UUID).
  3. Persist the self peer in the DB so the rest of the engine can
     reference it.
  4. Ensure the "default" room exists and self is a member — this is
     the auto-chat room any newly-discovered peer gets joined to.
  5. Start the mDNS discovery loop in a background thread. It
     advertises us and listens for other LocalLink nodes.
  6. Start the Flask HTTP listener in a background thread. Other peers
     POST encrypted bundles here.
  7. Launch the Textual TUI in the foreground. The TUI reads/writes
     the local DB directly via ``engine.api``; the background threads
     handle network I/O.

On Ctrl-C, we shut down discovery + Flask in a defined order so the
process exits cleanly without leaving the mDNS announcement stranded.
"""

import logging
import os
import signal
import sys
import threading
import time
import uuid


def _load_dotenv(path: str = ".env") -> None:
    """Populate ``os.environ`` from a ``.env`` file, if present.

    Existing environment variables win — we only fill in keys that are
    not already set (``setdefault`` semantics), so a real shell export
    always overrides the file. This must run BEFORE any engine imports,
    because several modules (``engine.storage.connection``,
    ``engine.security.keys``, ``engine.mesh.discovery``) read env vars
    at import time.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()

from engine.mesh import server as mesh_server
from engine.mesh.discovery import Discovery, DEFAULT_PORT
from engine.security.keys import (
    load_or_create_encryption_key,
    load_or_create_signing_key,
)
from engine.storage import database
from engine.storage.models import Peer


logger = logging.getLogger("locallink.run")


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    """Read an env var, stripping whitespace. Default when unset/empty."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip() or default


def _ensure_self_peer() -> Peer:
    """Load this node's peer_id from env, or generate one. Persist it.

    peer_id is what other peers will see in their DBs. It's stored
    nowhere on disk because re-deriving it from the env var on every
    launch keeps the bootstrap simple — set LOCALLINK_NODE_ID in
    .env to keep a stable id across restarts.
    """
    peer_id = _env("LOCALLINK_NODE_ID") or str(uuid.uuid4())
    name = _env("LOCALLINK_DISPLAY_NAME") or peer_id
    host = _env("LOCALLINK_HOST", "0.0.0.0")
    try:
        port = int(_env("LOCALLINK_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    encryption_key = load_or_create_encryption_key()
    public_key_hex = encryption_key.public_key.encode().hex()

    peer = database.save_peer({
        "peer_id": peer_id,
        "public_key": public_key_hex,
        "name": name,
        "ip_address": _get_local_ip(),
        "port": port,
        "is_online": True,
    })
    logger.info("self peer ready: id=%s name=%s", peer.peer_id, peer.name)
    return peer


def _get_local_ip() -> str:
    """Discover this machine's LAN IP using the UDP socket trick.
    Falls back to 127.0.0.1 when offline.
    """
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _ensure_default_room(creator_id: str):
    """Create the "default" room if it doesn't exist, and add creator
    as the first member.

    This is the auto-chat room: any peer that contacts us for the
    first time and addresses the default room will be auto-joined by
    the server (see ``mesh.server._ensure_member``).
    """
    existing = database.get_room_by_name("default")
    if existing is not None:
        return existing
    room = database.create_room(
        creator_id=creator_id,
        name="default",
        is_public=True,
        password_hash=None,
    )
    if room is None:
        raise RuntimeError("could not create default room; check DB layer")
    logger.info("created default room: %s", room.room_id)
    return room


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------


def _start_discovery(self_peer: Peer) -> Discovery:
    """Start mDNS in a background thread. Returns the Discovery instance
    so the caller can stop it on shutdown.
    """
    def on_peer_found(data: dict) -> None:
        # Mark the peer online and persist. The TUI polls list_peers()
        # to pick this up on its next refresh tick.
        data["is_online"] = True
        database.save_peer(data)
        logger.info("discovered peer: %s @ %s:%s",
                    data["peer_id"], data["ip_address"], data["port"])

    def on_peer_lost(peer_id: str) -> None:
        database.mark_peer_offline(peer_id)
        logger.info("peer went offline: %s", peer_id)

    discovery = Discovery(
        peer_id=self_peer.peer_id,
        public_key=self_peer.public_key,
        port=self_peer.port,
        host=None,
        on_peer_found=on_peer_found,
        on_peer_lost=on_peer_lost,
        name=self_peer.name,
    )
    if not discovery.start():
        raise RuntimeError("discovery failed to start")
    return discovery


def _start_flask(host: str, port: int) -> threading.Thread:
    """Start the Flask listener in a daemon thread.

    ``threaded=True`` so the dev server can handle concurrent requests
    (room-join and message-receive happening close together is
    realistic on a busy mesh). Debug is off — Werkzeug's reloader
    would spawn a second process and break the SQLite single-writer.
    """
    def _run():
        mesh_server.app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)

    t = threading.Thread(target=_run, name="locallink-flask", daemon=True)
    t.start()
    # Tiny grace period so the listener is actually accepting
    # connections before the TUI starts firing requests at it.
    time.sleep(0.3)
    return t


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOCALLINK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Initialize the database. Safe to call repeatedly.
    database.init_db()

    # 2-3. Self peer.
    self_peer = _ensure_self_peer()

    # 4. Default room.
    _ensure_default_room(self_peer.peer_id)

    # 5. Discovery.
    discovery = _start_discovery(self_peer)

    # 6. Flask HTTP listener.
    flask_thread = _start_flask(
        host=_env("LOCALLINK_HOST", "0.0.0.0"),
        port=self_peer.port,
    )

    # Wire signal handlers so Ctrl-C / SIGTERM shut things down in order.
    shutdown_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("received signal %s, shutting down", signum)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            # signal can only be installed from the main thread; if run.py
            # is imported, this is a no-op rather than a hard error.
            pass

    # 7. TUI in the foreground. The TUI takes over the terminal and
    #    blocks until the user quits. We import it lazily so the
    #    discovery + flask threads are already up by the time it runs.
    try:
        from cli.main import LocalLinkApp
        app = LocalLinkApp(self_peer_id=self_peer.peer_id)
        app.run()
    except ImportError:
        logger.warning("cli.main not importable; running headless (Ctrl-C to quit)")
        shutdown_event.wait()
    except Exception:
        logger.exception("TUI crashed")
        return 1
    finally:
        logger.info("stopping discovery")
        try:
            discovery.stop()
        except Exception:
            logger.exception("error stopping discovery")

    return 0


if __name__ == "__main__":
    sys.exit(main())
