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
import sys
import threading
import time


# ---------------------------------------------------------------------------
# .env loader (must run before engine imports read env vars)
# ---------------------------------------------------------------------------


def _dotenv_candidates(path: str = ".env") -> list[str]:
    """Return the list of paths to search for a .env file, in priority
    order. The first one that exists wins.

    Search order:
      1. ``sys._MEIPASS/<path>`` — PyInstaller --onefile extraction dir.
         This is where bundled data files (e.g. the .env we ship via
         --add-data) land at runtime.
      2. Directory of ``sys.executable`` — for cases where the .env sits
         next to the .exe (e.g. unpacked .exe, dev mode).
      3. CWD — the legacy fallback for ``python run.py`` from the repo.
    """
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, path))
        if getattr(sys, "executable", None):
            candidates.append(os.path.join(os.path.dirname(sys.executable), path))
    candidates.append(path)
    return candidates


def _load_dotenv(path: str = ".env") -> None:
    """Populate ``os.environ`` from a ``.env`` file.

    Searches ``sys._MEIPASS``, the .exe's directory, then CWD. The
    first existing file wins. Existing env vars are NOT overridden
    (setdefault semantics), so a real shell export always beats the
    file. This must run BEFORE any engine imports, because several
    modules (``engine.storage.connection``, ``engine.security.keys``,
    ``engine.mesh.discovery``) read env vars at import time.
    """
    for candidate in _dotenv_candidates(path):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        os.environ.setdefault(key, value)
            return
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
    """Discover this machine's LAN IP.

    Tries several methods in order, returning the first non-loopback
    IPv4 it can find. UDP "connect" is local-only — no packets are
    actually sent — so trying public IPs is safe even when offline.

    Order:
      1. UDP socket trick with public DNS IPs (8.8.8.8, 1.1.1.1) —
         picks the interface with a default route, which is almost
         always the LAN one.
      2. UDP socket trick with common LAN gateways — useful on
         air-gapped networks where 8.8.8.8 isn't routable but the
         LAN still has a default route.
      3. ``getaddrinfo(gethostname())`` filtered to skip loopback —
         works on most Windows machines on a LAN.
      4. Fall back to 127.0.0.1.

    The previous implementation used 10.255.255.255, which is
    non-routable on most OSes — the kernel can't pick a source
    address for it and we'd silently fall back to 127.0.0.1. That's
    the "we can see each other in the peer list but messages don't
    deliver" symptom, because the mDNS announcement would advertise
    127.0.0.1 as our address and other peers would try to connect to
    their own loopback.
    """
    import socket as _socket

    # 1. Public DNS IPs — gives the interface with the default route.
    for target in ("8.8.8.8", "1.1.1.1", "208.67.222.222"):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()

    # 2. Common LAN gateways — covers air-gapped LANs.
    for gateway in ("192.168.1.1", "192.168.0.1", "10.0.0.1", "172.16.0.1"):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect((gateway, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()

    # 3. Hostname lookup — works on most Windows LAN machines.
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            addr = info[4][0]
            if addr and not addr.startswith("127."):
                return addr
    except Exception:
        pass

    logger.warning(
        "Could not determine a non-loopback LAN IP; using 127.0.0.1. "
        "Other peers will not be able to reach this node until the "
        "network interface comes up."
    )
    return "127.0.0.1"


def _wait_for_writer() -> None:
    """Block until the SqliteQueueDatabase writer thread has drained.

    ``create_tables()`` and ``Model.create()`` both queue their work
    on a background thread. If we do a SELECT immediately after
    queuing a CREATE, the SELECT may run on a different connection
    that doesn't yet see the committed DDL — hence
    ``no such table: peer`` and friends. This helper spins on the
    queue's emptiness flag until the writer is caught up. Resolves
    in a few milliseconds in practice.
    """
    from engine.storage.connection import get_db
    db = get_db()
    queue = getattr(db, "_write_queue", None)
    if queue is None:
        return
    # The writer thread processes items in microseconds; cap the
    # wait at ~2s so a wedged writer doesn't hang the whole app.
    deadline = time.time() + 2.0
    while not queue.empty() and time.time() < deadline:
        time.sleep(0.01)


def _ensure_self_peer() -> Peer:
    """Load this node's peer_id from env, or generate one. Persist it.

    Uses direct model operations (``Peer.create`` / ``Peer.get_or_none``)
    and waits for the writer thread to drain after the DDL, so a
    SELECT right after a CREATE doesn't race the schema setup.
    """
    from engine.storage.models import Peer as PeerModel
    from engine.storage.connection import get_db

    db = get_db()
    db.connect(reuse_if_open=True)
    db.create_tables([PeerModel], safe=True)
    _wait_for_writer()

    encryption_key = load_or_create_encryption_key()
    public_key_hex = encryption_key.public_key.encode().hex()
    
    signing_key = load_or_create_signing_key()
    # Use a short hash of the signing key as the peer_id.
    # The full 64-char hex exceeds Zeroconf's 63-char DNS label limit,
    # which crashes mDNS registration. 12 hex chars = 48 bits of
    # uniqueness — more than enough to avoid collisions in a demo setting.
    import hashlib
    key_hash = hashlib.sha256(bytes(signing_key)).hexdigest()[:12]
    peer_id = _env("LOCALLINK_NODE_ID") or f"ll-{key_hash}"
    
    try:
        port = int(_env("LOCALLINK_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    existing = PeerModel.get_or_none(PeerModel.peer_id == peer_id)
    if existing is not None:
        if _env("LOCALLINK_DISPLAY_NAME"):
            existing.name = _env("LOCALLINK_DISPLAY_NAME")
        existing.public_key = public_key_hex
        existing.ip_address = _get_local_ip()
        existing.port = port
        existing.is_online = True
        # Touch last_active so prune_stale_peers() never evicts the
        # self peer (which mDNS reports back to us, but not via this
        # callback).
        existing.last_active = time.time()
        existing.save()
        peer = existing
    else:
        # Only set a name when the user explicitly provided one.
        # Leaving it empty triggers the TUI's SetupModal on first
        # run, instead of leaking the auto-generated peer_id as the
        # display name (which then propagates into mDNS TXT records
        # and other peers' UIs).
        name = _env("LOCALLINK_DISPLAY_NAME") or ""
        peer = PeerModel.create(
            peer_id=peer_id,
            public_key=public_key_hex,
            name=name,
            ip_address=_get_local_ip(),
            port=port,
            is_online=True,
            last_active=time.time(),
        )
    _wait_for_writer()
    logger.info("self peer ready: id=%s name=%s", peer.peer_id, peer.name)
    return peer


def _ensure_default_room(creator_id: str):
    """Create the "default" room if it doesn't exist, and add creator
    as the first member.
    """
    from engine.storage.models import Room as RoomModel, RoomMember
    from engine.storage.connection import get_db

    db = get_db()
    db.connect(reuse_if_open=True)
    db.create_tables([RoomModel, RoomMember], safe=True)
    _wait_for_writer()

    existing = RoomModel.get_or_none(RoomModel.name == "default")
    if existing is not None:
        return existing
    room = RoomModel.create(
        name="default",
        creator_id=creator_id,
        is_public=True,
        password_hash=None,
    )
    RoomMember.create(room=room, peer_id=creator_id, role="admin")
    _wait_for_writer()
    logger.info("created default room: %s", room.room_id)
    return room


def _start_discovery(self_peer: Peer) -> Discovery:
    """Start mDNS in a background thread."""
    def on_peer_found(data: dict) -> None:
        """Called when mDNS reports a new (or refreshed) peer.

        Dedups by public key: if the same public key is already known
        under a different peer_id, we collapse to the existing row and
        drop the stale alias. This handles the case where the same
        physical machine announces itself with different identities
        (e.g. a previous .exe run with different key files), which
        would otherwise leave us with multiple rows for one machine
        and cause the auto-mirror to pull the same rooms multiple
        times.
        """
        discovered_public_key = (data.get("public_key") or "").strip()
        discovered_peer_id = (data.get("peer_id") or "").strip()

        if discovered_public_key:
            existing = database.get_peer_by_public_key(discovered_public_key)
            if (
                existing is not None
                and existing.peer_id != discovered_peer_id
            ):
                # Same machine, different peer_id. Rewrite the
                # announcement to use the existing (more stable)
                # peer_id, refresh network info, prefer a non-auto
                # generated name, then drop the stale alias.
                new_name = (data.get("name") or "").strip()
                if new_name == discovered_peer_id:
                    new_name = ""  # auto-generated, skip
                merged = {
                    "peer_id": existing.peer_id,
                    "public_key": discovered_public_key,
                    "ip_address": data.get("ip_address") or existing.ip_address,
                    "port": data.get("port") or existing.port,
                    "name": new_name or existing.name,
                    "is_online": True,
                }
                try:
                    database.delete_peer(discovered_peer_id)
                except Exception:
                    logger.exception(
                        "failed to delete stale peer alias %s",
                        discovered_peer_id,
                    )
                data = merged

        if not data.get("peer_id"):
            return
        # Touch last_active on every mDNS refresh so the
        # prune_stale_peers() call in the TUI can correctly identify
        # alive peers vs. ones that have silently dropped.
        data["is_online"] = True
        data["last_active"] = time.time()
        database.save_peer(data)
        logger.info(
            "discovered peer: %s @ %s:%s",
            data["peer_id"], data["ip_address"], data["port"],
        )

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
    try:
        if not discovery.start():
            logger.warning("Discovery returned False — running without mDNS (offline mode)")
    except Exception:
        logger.exception("Discovery failed to start — running without mDNS (offline mode)")
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
    _wait_for_writer()

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
