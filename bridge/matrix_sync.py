"""
bridge/matrix_sync.py

Asynchronous Matrix Gateway bridge.

Purpose
-------
Push locally-stored, unsynced messages (``is_synced = False``) from the
LocalLink SQLite ledger into a federated Matrix room. The bridge only
runs when the host has internet access; while offline, messages simply
accumulate in the local DB and get picked up on the next successful
sync cycle.

Why a bot account on a public homeserver?
-----------------------------------------
For a hackathon deployment, running your own Matrix homeserver is
overkill. Instead we log in as a bot user on any public homeserver
(matrix.org, envs.net, etc.), join a regular room, and use the
Client-Server REST API directly with ``requests``. No async runtime,
no extra runtime deps, no homeserver to babysit.

Flow per sync cycle
-------------------
1.  ``has_internet()`` — cheap connectivity probe (TCP connect to
    homeserver port 443). We never run the API path if we're offline.
2.  ``list_unsynced_messages()`` — pull everything still flagged
    ``is_synced = False``, oldest first.
3.  For each message, resolve the target Matrix room:
        - If the local ``Room`` row has ``matrix_room_id`` set, use it.
        - Otherwise fall back to the ``MATRIX_ROOM_ID`` env var.
        - If neither is set, the message is skipped (logged) — we
          don't want to spam the wrong room.
4.  ``send_message()`` — PUT the message to
    ``/_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}``.
    The ``txnId`` is the local ``message_id`` so retries are
    idempotent on Matrix's side.
5.  On HTTP 200, ``mark_message_synced(message_id, event_id)`` so we
    don't push it again. On any failure, leave the row alone — it'll
    be retried on the next cycle.

Failure handling (matches the rest of the engine)
-------------------------------------------------
- Narrow exception catches (``ConnectionError``, ``Timeout``). A dead
  Matrix endpoint must not crash the worker thread.
- Short timeout (10s) per request. One slow call shouldn't stall the
  whole queue.
- We never ``mark_message_synced`` on anything but a confirmed 200
  response. A 4xx/5xx leaves the message unsynced for the next cycle.

Auth
----
This module expects a long-lived ``MATRIX_ACCESS_TOKEN`` in the
environment. To get one:

    # one-time, on any machine with curl
    curl -X POST https://matrix.org/_matrix/client/v3/login \
         -H 'Content-Type: application/json' \
         -d '{"type":"m.login.password",
              "user":"<your-bot-user>",
              "password":"<password>"}'

    # grab "access_token" out of the response. It's valid until logout.

For a hackathon demo, a personal account's token is fine. For anything
real, create a dedicated bot account.
"""

import logging
import os
import signal
import socket
import time
import uuid
from typing import Optional

import requests

from engine.storage import database
from engine.storage.models import Message, Room

logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_SYNC_INTERVAL_SECONDS = 10
CONNECTIVITY_PROBE_TIMEOUT_SECONDS = 3


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an env var, stripping whitespace. None when unset/empty."""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def _required_env(name: str) -> str:
    """Read a required env var, raising a clear error when missing."""
    val = _env(name)
    if not val:
        raise RuntimeError(
            f"Matrix bridge: required env var {name} is not set. "
            f"See .env.example for the full list."
        )
    return val


def has_internet(host: str = "matrix.org", port: int = 443) -> bool:
    """Return True if we can open a TCP socket to ``host:port``.

    Cheap connectivity probe — does NOT speak HTTP. We use this to skip
    the API path entirely when offline so we don't waste cycles
    logging connection errors. A successful probe is not a guarantee
    that the full Matrix API is reachable, but it's good enough to
    gate the worker loop.
    """
    try:
        with socket.create_connection((host, port), timeout=CONNECTIVITY_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _resolve_matrix_room_id(room: Room, fallback: Optional[str]) -> Optional[str]:
    """Pick the Matrix room to push a message into.

    Preference order:
        1. ``room.matrix_room_id`` if set on the local Room row.
        2. ``fallback`` — the default from ``MATRIX_ROOM_ID`` env.

    Returns None when neither is configured; the caller should skip
    the message in that case rather than spam an arbitrary room.
    """
    if room.matrix_room_id:
        return room.matrix_room_id
    return fallback


def send_message(
    homeserver_url: str,
    access_token: str,
    matrix_room_id: str,
    body: str,
    txn_id: str,
    sender_display: Optional[str] = None,
) -> Optional[str]:
    """PUT a single ``m.room.message`` event to Matrix.

    Returns the Matrix event_id on success, None on failure. The
    ``txn_id`` is what makes the call idempotent — Matrix dedupes
    based on (sender, txn_id) for ~24h, so retries of the same local
    message won't create duplicate Matrix events.
    """
    url = (
        f"{homeserver_url.rstrip('/')}"
        f"/_matrix/client/v3/rooms/{matrix_room_id}"
        f"/send/m.room.message/{txn_id}"
    )
    payload = {
        "msgtype": "m.text",
        "body": body,
    }
    if sender_display:
        # Lets Element render "alice: hello" instead of just "hello".
        # Purely cosmetic — does not affect auth or federation.
        payload["body"] = f"{sender_display}: {body}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.put(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.ConnectionError:
        logger.warning("matrix: connection refused (%s)", url)
        return None
    except requests.exceptions.Timeout:
        logger.warning("matrix: timed out after %ss (%s)", REQUEST_TIMEOUT_SECONDS, url)
        return None
    except requests.exceptions.RequestException as e:
        logger.warning("matrix: request failed (%s): %s", type(e).__name__, e)
        return None

    if resp.status_code != 200:
        logger.warning(
            "matrix: rejected message: %s -> %d %s",
            url, resp.status_code, resp.text[:200],
        )
        return None

    try:
        return resp.json().get("event_id")
    except ValueError:
        logger.warning("matrix: 200 OK but unparseable JSON: %s", resp.text[:200])
        return None


def sync_once(
    homeserver_url: str,
    access_token: str,
    default_room_id: Optional[str] = None,
    peer_display_names: Optional[dict] = None,
) -> int:
    """Run one sync pass. Returns the number of messages successfully pushed.

    Designed to be called either by the worker's loop or by tests that
    want to drive a single cycle without spawning a thread. All side
    effects on the DB go through ``engine.storage.database`` — this
    function does not touch peewee models directly except to read.
    """
    if peer_display_names is None:
        peer_display_names = {}

    pending = database.list_unsynced_messages()
    if not pending:
        return 0

    pushed = 0
    for msg in pending:
        room = msg.room
        matrix_room_id = _resolve_matrix_room_id(room, default_room_id)
        if not matrix_room_id:
            logger.warning(
                "matrix: skipping message %s — no matrix_room_id on room %s "
                "and no MATRIX_ROOM_ID fallback",
                msg.message_id, room.room_id,
            )
            continue

        display = peer_display_names.get(msg.sender_id, msg.sender_id)
        event_id = send_message(
            homeserver_url=homeserver_url,
            access_token=access_token,
            matrix_room_id=matrix_room_id,
            body=msg.content,
            txn_id=msg.message_id,
            sender_display=display,
        )
        if event_id is None:
            continue

        if database.mark_message_synced(msg.message_id, event_id):
            pushed += 1
        else:
            logger.error("matrix: sent OK but could not mark synced: %s", msg.message_id)

    return pushed


class MatrixBridge:
    """Long-running worker that drains the unsynced queue to Matrix.

    Usage:
        bridge = MatrixBridge()
        bridge.run()           # blocks until KeyboardInterrupt
    """

    def __init__(
        self,
        homeserver_url: Optional[str] = None,
        access_token: Optional[str] = None,
        default_room_id: Optional[str] = None,
        sync_interval: Optional[int] = None,
    ) -> None:
        self.homeserver_url = (
            homeserver_url
            or _env("MATRIX_HOMESERVER_URL")
            or "https://matrix.org"
        ).rstrip("/")
        self.access_token = (
            access_token or _env("MATRIX_ACCESS_TOKEN") or ""
        ).strip()
        if not self.access_token:
            raise RuntimeError(
                "MatrixBridge: MATRIX_ACCESS_TOKEN is required "
                "(see .env.example)."
            )
        self.default_room_id = (
            default_room_id
            or _env("MATRIX_ROOM_ID")
        )
        interval_str = (
            sync_interval
            if sync_interval is not None
            else _env("MATRIX_SYNC_INTERVAL", str(DEFAULT_SYNC_INTERVAL_SECONDS))
        )
        try:
            self.sync_interval = max(1, int(interval_str or DEFAULT_SYNC_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            self.sync_interval = DEFAULT_SYNC_INTERVAL_SECONDS

        self._stop = False

    def stop(self) -> None:
        """Signal the worker loop to exit after the current cycle."""
        self._stop = True

    def _install_signal_handlers(self) -> None:
        """Make Ctrl-C stop the worker cleanly between cycles."""
        def _handler(signum, frame):
            logger.info("matrix: received signal %s, shutting down", signum)
            self._stop = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def run(self) -> None:
        """Block and run sync cycles forever, until ``stop()`` or a signal."""
        self._install_signal_handlers()
        logger.info(
            "matrix: bridge started (homeserver=%s, default_room=%s, interval=%ss)",
            self.homeserver_url, self.default_room_id, self.sync_interval,
        )
        while not self._stop:
            try:
                if not has_internet():
                    logger.debug("matrix: offline, sleeping")
                else:
                    pushed = sync_once(
                        homeserver_url=self.homeserver_url,
                        access_token=self.access_token,
                        default_room_id=self.default_room_id,
                    )
                    if pushed:
                        logger.info("matrix: pushed %d message(s)", pushed)
            except Exception:
                logger.exception("matrix: unexpected error in sync loop")
            for _ in range(self.sync_interval):
                if self._stop:
                    break
                time.sleep(1)
        logger.info("matrix: bridge stopped")


def _get_access_token_interactively(homeserver_url: str) -> str:
    """Prompt for username/password and return a fresh access token.

    Convenience for hackathon setup. Uses the ``m.login.password``
    flow. Not stored — caller is expected to put it in .env so the
    next run doesn't need to log in again.
    """
    import getpass
    user = input("Matrix user id (e.g. @bot:matrix.org): ").strip()
    password = getpass.getpass("Matrix password: ")
    url = f"{homeserver_url.rstrip('/')}/_matrix/client/v3/login"
    body = {
        "type": "m.login.password",
        "user": user,
        "password": password,
        "initial_device_display_name": "LocalLink Bridge",
    }
    resp = requests.post(url, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()["access_token"]


if __name__ == "__main__":
    """Manual entry point:

        python -m bridge.matrix_sync            # run worker
        python -m bridge.matrix_sync --login    # log in and print token
    """
    import argparse
    import sys

    logging.basicConfig(
        level=os.environ.get("LOCALLINK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="LocalLink Matrix bridge")
    parser.add_argument("--login", action="store_true", help="Log in and print a fresh access token")
    args = parser.parse_args()

    homeserver = (_env("MATRIX_HOMESERVER_URL") or "https://matrix.org").rstrip("/")

    if args.login:
        token = _get_access_token_interactively(homeserver)
        print("\nYour access token (paste this into .env as MATRIX_ACCESS_TOKEN):")
        print(token)
        sys.exit(0)

    bridge = MatrixBridge()
    bridge.run()
