"""
engine/bootstrap.py

Master entry point for the installed ``locallink`` command.

Why a module, not run.py?
  When ``pip install`` creates the console script, it runs
  ``python -c "from run import main"`` from the install location —
  but ``run.py`` lives at the project root, not in the installed
  package. Pointing the entry point at ``engine.bootstrap:run``
  guarantees the module is importable after install.

The dev entry point (``run.py`` at the project root) is a thin
wrapper around ``engine.bootstrap.run()`` — same code, no logic
duplication.

Bootstrap sequence:
  1. Load .env (must run before any engine imports).
  2. Initialize the SQLite database (create tables if missing).
  3. Load or generate this node's identity (signing + encryption
     keys) and its peer_id (from LOCALLINK_NODE_ID, or fresh UUID).
  4. Persist the self peer in the DB so the rest of the engine
     can reference it.
  5. Ensure the "default" room exists and self is a member.
  6. Start the mDNS discovery loop in a background thread.
  7. Start the Flask HTTP listener in a background thread.
  8. Launch the Textual TUI in the foreground.

On Ctrl-C, discovery + Flask are torn down in order.
"""

import logging
import os
import signal
import threading
import time
import uuid


# ---------------------------------------------------------------------------
# .env loader (must run before engine imports read env vars)
# ---------------------------------------------------------------------------


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


logger = logging.getLogger("locallink.bootstrap")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    """Read an env var, stripping whitespace. Default when unset/empty."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip() or default


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


def _ensure_self_peer() -> Peer:
    """Load this node's peer_id from env, or generate one. Persist it."""
    peer_id = _env("LOCALLINK_NODE_ID") or str(uuid.uuid4())
    name = _env("LOCALLINK_DISPLAY_NAME") or peer_id
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


def _ensure_default_room(creator_id: str):
    """Create the "default" room if it doesn't exist, and add creator
    as the first member.
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


def _start_discovery(self_peer: Peer) -> Discovery:
    """Start mDNS in a background thread."""
    def on_peer_found(data: dict) -> None:
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
    """Start the Flask listener in a daemon thread."""
    def _run():
        mesh_server.app.run(
            host=host, port=port, debug=False,
            threaded=True, use_reloader=False,
        )

    t = threading.Thread(target=_run, name="locallink-flask", daemon=True)
    t.start()
    time.sleep(0.3)
    return t


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> int:
    """Boot the full stack and hand the terminal to the TUI.

    This is the function the ``locallink`` console script points at.
    Returns the process exit code.
    """
    logging.basicConfig(
        level=os.environ.get("LOCALLINK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Initialize the database.
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
            pass

    # 7. TUI in the foreground.
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
