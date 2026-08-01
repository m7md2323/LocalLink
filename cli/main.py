"""
cli/main.py

Textual-based terminal UI for LocalLink.

Layout:
    +------------------------------------------------------------------+
    | LocalLink — node_local_01 · 2 peers · 2 rooms · ONLINE     10:32 |
    +-----------+------------------------------------------------------+
    | ROOMS     | # general                                            |
    |  default  | [10:32] alice  hey, you around?                      |
    |  general  | [10:33] you    yeah, what's up?                      |
    | + new (n) | [10:34] alice  wifi's flaky today                    |
    |           |                                                      |
    | PEERS     |                                                      |
    |  you      |                                                      |
    |  alice    | > _                                                  |
    +-----------+------------------------------------------------------+

Key design points:
  - Messages render in a RichLog (append-only, auto-scrolls) instead
    of a rebuilt ListView — no flicker, no scroll reset on the 1s tick.
  - The internet probe runs in a WORKER THREAD. Calling
    socket.create_connection on the UI thread froze the whole app for
    up to 3s every 5s whenever the mesh was offline.
  - Sending while offline still saves locally (offline-first): the row
    lands in the DB unsynced, the bridge can push it later. The send
    path is "save once, fan out to every online peer".
  - Room/peer lists rebuild on their refresh ticks WITHOUT stealing
    the user's selection highlight.
"""

import logging
import socket
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    Switch,
)

from engine import api
from engine.storage.models import Message, Room


logger = logging.getLogger(__name__)


CONNECTIVITY_PROBE_TIMEOUT_SECONDS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_internet() -> bool:
    """Cheap TCP probe — runs in a worker thread, never on the UI loop."""
    try:
        with socket.create_connection(("matrix.org", 443), timeout=CONNECTIVITY_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _format_message(msg: Message, peer_names: dict, self_peer_id: str) -> str:
    """Render one message line as plain text. No markup, no colors —
    classic green-on-black terminal aesthetic.
    """
    sender = peer_names.get(msg.sender_id) or msg.sender_id[:8]
    if msg.sender_id == self_peer_id:
        sender = "you"
    ts = time.strftime("%H:%M", time.localtime(msg.timestamp))
    return f"{ts}  {sender}  {msg.content}"


# ---------------------------------------------------------------------------
# Modal: "New Room"
# ---------------------------------------------------------------------------


class NewRoomModal(ModalScreen[Optional[dict]]):
    """Modal for creating a new room.

    The password field is hidden until the Private switch is flipped.
    Enter in the name field submits. Escape cancels.

    Returns ``{"name": str, "is_public": bool, "password": str | None}``
    on submit, or None on cancel.
    """

    DEFAULT_CSS = """
    NewRoomModal {
        align: center middle;
        background: rgba(24, 24, 37, 0.8);
    }
    #new-room-dialog {
        width: 52;
        height: auto;
        border: round #89b4fa;
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 1 2;
    }
    #new-room-dialog .title {
        text-style: bold;
        color: #89b4fa;
    }
    #new-room-dialog Label {
        margin-top: 1;
        color: #cdd6f4;
    }
    #row-private {
        height: auto;
        margin-top: 1;
    }
    #row-private Static {
        margin-right: 1;
        margin-top: 1;
        color: #cdd6f4;
    }
    #password-block {
        display: none;
    }
    #password-block.visible {
        display: block;
    }
    #modal-buttons {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }
    #modal-buttons Button {
        margin-left: 1;
        background: #313244;
        color: #cdd6f4;
        border: none;
    }
    #modal-buttons Button.primary {
        background: #89b4fa;
        color: #11111b;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="new-room-dialog"):
            yield Static("Create a new room", classes="title")
            yield Label("Name:")
            yield Input(id="input-name", placeholder="e.g. general")
            with Horizontal(id="row-private"):
                yield Static("Private")
                yield Switch(id="switch-private", value=False)
            with Vertical(id="password-block"):
                yield Label("Password:")
                yield Input(id="input-password", password=True)
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Create", id="btn-create", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "switch-private":
            block = self.query_one("#password-block")
            block.set_class(event.value, "visible")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in either field acts like clicking Create.
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        if event.button.id == "btn-create":
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#input-name", Input).value.strip()
        is_private = self.query_one("#switch-private", Switch).value
        password = self.query_one("#input-password", Input).value or None
        if not name:
            self.notify("Name cannot be empty", severity="warning")
            return
        if is_private and not password:
            self.notify("Private rooms need a password", severity="warning")
            return
        self.dismiss({
            "name": name,
            "is_public": not is_private,
            "password": password,
        })


# ---------------------------------------------------------------------------
# Modal: "Setup Name"
# ---------------------------------------------------------------------------

class SetupModal(ModalScreen[Optional[str]]):
    """Modal to prompt for display name on first run."""

    DEFAULT_CSS = """
    SetupModal {
        align: center middle;
        background: rgba(24, 24, 37, 0.8);
    }
    #setup-dialog {
        width: 40;
        height: auto;
        border: round #89b4fa;
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 1 2;
    }
    #setup-dialog .title {
        text-style: bold;
        color: #89b4fa;
    }
    #setup-dialog Label {
        margin-top: 1;
        color: #cdd6f4;
    }
    #setup-buttons {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }
    #setup-buttons Button {
        margin-left: 1;
        background: #313244;
        color: #cdd6f4;
        border: none;
    }
    #setup-buttons Button.primary {
        background: #89b4fa;
        color: #11111b;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-dialog"):
            yield Static("Welcome to LocalLink!", classes="title")
            yield Label("Enter your Display Name:")
            yield Input(id="input-display-name", placeholder="e.g. Alice")
            with Horizontal(id="setup-buttons"):
                yield Button("Join Mesh", id="btn-join", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#input-display-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-join":
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#input-display-name", Input).value.strip()
        if not name:
            self.notify("Name cannot be empty", severity="warning")
            return
        self.dismiss(name)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class LocalLinkApp(App):
    """The full LocalLink terminal UI."""

    CSS = """
    Screen {
        background: #1e1e2e;
        color: #cdd6f4;
    }
    #main-area {
        height: 1fr;
    }
    #sidebar {
        width: 26;
        background: #181825;
        color: #cdd6f4;
        border-right: solid #313244;
        padding: 0 1;
    }
    #sidebar > Static {
        color: #a6adc8;
    }
    #chat-area {
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 0 1;
    }
    #chat-title {
        color: #89b4fa;
        text-style: bold;
        margin-bottom: 1;
    }
    #message-log {
        height: 1fr;
        background: #1e1e2e;
        color: #cdd6f4;
        border: none;
        padding: 0 1;
    }
    #input-box {
        height: 3;
        background: #181825;
        color: #cdd6f4;
        border: solid #313244;
    }
    ListView {
        background: #181825;
        color: #cdd6f4;
    }
    ListView > ListItem {
        background: #181825;
        color: #cdd6f4;
    }
    ListView > ListItem.--highlight {
        background: #313244;
        color: #cdd6f4;
    }
    Header {
        background: #11111b;
        color: #cdd6f4;
    }
    Footer {
        background: #11111b;
        color: #cdd6f4;
    }
    """

    # NOTE on key choices: plain printable keys (n, j, ?) get eaten by
    # the focused Input widget before App-level bindings ever see them,
    # so all bindings here are non-printable (ctrl/f-key).
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+n", "new_room", "New room"),
        ("f1", "help", "Help"),
    ]

    selected_room_id: reactive[Optional[str]] = reactive(None)
    is_online: reactive[bool] = reactive(False)

    def __init__(self, self_peer_id: str) -> None:
        super().__init__()
        self.self_peer_id = self_peer_id
        self._rooms_cache: list[Room] = []
        self._peer_names: dict[str, str] = {}
        # Number of messages already written to the RichLog for the
        # currently selected room. Reset on room switch so the log
        # re-renders from scratch.
        self._rendered_count: int = 0
        # Per-peer timestamp of the last room-list mirror. We re-pull a
        # peer's public rooms at most every MIRROR_INTERVAL seconds so a
        # peer that creates a new room while we're both online gets
        # picked up without spamming /api/rooms every refresh tick.
        self._mirrored_peers: dict[str, float] = {}

    # ---- Compose ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="sidebar"):
                yield Static("ROOMS", classes="section-title")
                yield ListView(id="room-list")
                yield Static("+ new room (ctrl+n)", classes="hint")
                yield Static("PEERS", classes="section-title")
                yield ListView(id="peer-list")
            with Vertical(id="chat-area"):
                yield Static("Select a room", id="chat-title")
                yield RichLog(id="message-log", markup=False, auto_scroll=True, wrap=True)
                yield Input(id="input-box", placeholder="Type a message and press Enter…")
        yield Footer()

    # ---- Lifecycle --------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "LocalLink"
        self.sub_title = "starting…"
        self._refresh_rooms()
        self._refresh_peers()
        self._refresh_messages(force=True)
        self._refresh_status()
        self.set_interval(1.0, self._refresh_messages)
        self.set_interval(3.0, self._refresh_rooms_and_peers)
        self.set_interval(5.0, self._refresh_status)
        self.query_one("#input-box", Input).focus()

        self_peer = api.get_self_peer()
        # Trigger the setup modal if either (a) the name is the
        # auto-generated peer_id (legacy behaviour) or (b) the name is
        # empty (the new bootstrap default that lets us prompt the
        # user instead of leaking the peer_id into the UI).
        if self_peer and (not self_peer.name or self_peer.name == self_peer.peer_id):
            def _on_setup_dismiss(name: Optional[str]) -> None:
                if name:
                    api.update_self_name(self.self_peer_id, name)
                    self._refresh_peers()
            self.push_screen(SetupModal(), _on_setup_dismiss)

    # ---- Refresh loops ----------------------------------------------------

    def _refresh_rooms(self) -> None:
        """Pull the room list and re-render the sidebar, preserving the
        user's current selection highlight across rebuilds."""
        rooms = api.list_rooms_for(self.self_peer_id)
        self._rooms_cache = rooms
        lv = self.query_one("#room-list", ListView)
        lv.clear()
        for room in rooms:
            marker = "> " if room.name == "default" else "  "
            lock = " (private)" if not room.is_public else ""
            lv.append(ListItem(Static(f"{marker}{room.name}{lock}")))
        if not rooms:
            return
        # Restore selection by room_id; fall back to the first room.
        idx = 0
        if self.selected_room_id is not None:
            for i, room in enumerate(rooms):
                if room.room_id == self.selected_room_id:
                    idx = i
                    break
        if self.selected_room_id is None:
            self.selected_room_id = rooms[0].room_id
            self.query_one("#chat-title", Static).update(f"# {rooms[0].name}")
        lv.index = idx

    def _refresh_peers(self) -> None:
        """Pull the peer list and update the name cache for the chat."""
        # Mark peers offline if mDNS hasn't refreshed them in a while.
        # Compensates for the lazy remove_service notification that
        # otherwise keeps ghost peers showing "online" for 1+ minutes.
        api.prune_stale_peers(max_age_seconds=90.0)
        peers = api.list_peers()
        self._peer_names = {p.peer_id: (p.name or p.peer_id[:8]) for p in peers}
        lv = self.query_one("#peer-list", ListView)
        lv.clear()
        for p in peers:
            if p.peer_id == self.self_peer_id:
                label = "> you"
            else:
                name = p.name or p.peer_id[:8]
                status = "online" if p.is_online else "offline"
                label = f"  {name} ({status})"
            lv.append(ListItem(Static(label)))

    def _auto_mirror_remote_rooms(self) -> None:
        """Pull each online peer's public rooms and mirror any new ones
        locally so the sidebar populates without the user having to type
        ``/j <room_id>`` by hand.

        Idempotent: ``mirror_remote_room`` returns the existing row if
        the room is already mirrored, and ``ensure_membership`` is a
        no-op if we're already a member. A peer is re-queried at most
        every ``MIRROR_INTERVAL`` seconds, so a peer that creates a
        new room while both nodes are online gets picked up promptly
        without spamming ``GET /api/rooms`` on every 3s tick.
        """
        MIRROR_INTERVAL = 30.0
        now = time.time()
        online = [p for p in api.list_online_peers() if p.peer_id != self.self_peer_id]
        for peer in online:
            last = self._mirrored_peers.get(peer.peer_id, 0.0)
            if now - last < MIRROR_INTERVAL:
                continue
            try:
                remote_rooms = api.list_remote_rooms(peer)
            except Exception:
                self._mirrored_peers[peer.peer_id] = now
                continue
            for room_meta in remote_rooms:
                try:
                    mirrored = api.mirror_remote_room(peer, room_meta)
                except Exception:
                    continue
                if mirrored is not None:
                    api.ensure_membership(mirrored.room_id, self.self_peer_id)
            self._mirrored_peers[peer.peer_id] = now

    def _refresh_rooms_and_peers(self) -> None:
        self._auto_mirror_remote_rooms()
        self._refresh_rooms()
        self._refresh_peers()
        # Names may have changed (new peers via mDNS) — re-render the
        # tail of the log cheaply by forcing a full repaint of the room.
        self._refresh_messages(force=True)

    def _refresh_messages(self, force: bool = False) -> None:
        """Append any new messages of the selected room to the RichLog.

        Incremental: we track how many messages we've already written
        and only append the tail. ``force=True`` (room switch, peer
        rename) clears the log and re-renders everything.
        """
        room_id = self.selected_room_id
        if not room_id:
            return
        messages = api.list_messages(room_id, self.self_peer_id)
        log = self.query_one("#message-log", RichLog)
        if force:
            log.clear()
            self._rendered_count = 0
        if len(messages) < self._rendered_count:
            # Room was reset (or messages deleted) — repaint.
            log.clear()
            self._rendered_count = 0
        for msg in messages[self._rendered_count:]:
            log.write(_format_message(msg, self._peer_names, self.self_peer_id))
        self._rendered_count = len(messages)

    def _refresh_status(self) -> None:
        """Kick off the connectivity probe in a worker thread.

        The probe does a blocking socket connect (up to 3s); running it
        on the UI thread froze the whole app. ``exclusive=True`` keeps
        ticks from stacking while a probe is in flight.
        """
        self.run_worker(self._probe_and_apply, thread=True, exclusive=True)

    def _probe_and_apply(self) -> None:
        online = _has_internet()
        self.call_from_thread(self._apply_status, online)

    def _apply_status(self, online: bool) -> None:
        self.is_online = online
        peer_count = len(api.list_online_peers())
        room_count = len(self._rooms_cache)
        status = "ONLINE" if online else "OFFLINE"
        self.sub_title = f"{peer_count} peers · {room_count} rooms · {status}"

    # ---- Selection handling ----------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Switch the active room when the user arrows/clicks the list.

        Resolved via list index (not widget ids) so refresh rebuilds
        can't break the mapping.
        """
        if event.list_view.id != "room-list":
            return
        idx = event.list_view.index
        if idx is None or idx < 0 or idx >= len(self._rooms_cache):
            return
        room = self._rooms_cache[idx]
        if room.room_id != self.selected_room_id:
            self.selected_room_id = room.room_id
            self.query_one("#chat-title", Static).update(f"# {room.name}")
            self._refresh_messages(force=True)

    # ---- Sending messages -------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "input-box":
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if text.startswith("/"):
            self._handle_command(text)
            return
        room_id = self.selected_room_id
        if not room_id:
            self.notify("Select a room first", severity="warning")
            return
        self._send_message(room_id, text)

    def _send_message(self, room_id: str, text: str) -> None:
        """Offline-first send: persist locally immediately (fast DB
        write), then fan out to online peers in a background worker
        thread so a slow or dead peer never freezes the UI.

        The local copy is the durable record; network delivery is
        best-effort per peer.
        """
        saved = api.save_outgoing(room_id, text, self.self_peer_id)
        if saved is None:
            self.notify("Could not save message (not a member of this room?)",
                        severity="error")
            return
        self._refresh_messages(force=True)

        online = [p for p in api.list_online_peers() if p.peer_id != self.self_peer_id]
        if not online:
            self.notify("Saved locally — no peers online", severity="information")
            return

        self.run_worker(
            lambda: self._deliver_to_peers(room_id, text, list(online)),
            thread=True, exclusive=False,
        )

    def _deliver_to_peers(self, room_id: str, text: str, peers: list) -> None:
        """Run in a worker thread; POST the message to each online
        peer, then notify the UI of the outcome."""
        delivered = sum(
            1 for p in peers
            if api.deliver_to_peer(p, room_id, text, self.self_peer_id)
        )
        if delivered == 0:
            self.call_from_thread(
                lambda: self.notify(
                    "Saved locally — peers unreachable", severity="warning"
                )
            )

    # ---- Slash commands ---------------------------------------------------

    def _handle_command(self, text: str) -> None:
        """Dispatch slash commands typed into the input box."""
        if text.startswith("/j "):
            self._handle_join_command(text[3:].strip())
        elif text == "/leave":
            self._handle_leave_command()
        elif text in ("/quit", "/q"):
            self.exit()
        elif text in ("/help", "/h", "/?"):
            self.notify(
                "/j <room_id> [password] — join a remote room · "
                "/leave — leave current room · "
                "/q — quit · n — new room · ? — this help",
                title="Commands",
            )
        else:
            self.notify(f"Unknown command: {text} (try /help)", severity="warning")

    def _handle_join_command(self, arg: str) -> None:
        """``/j <room_id> [password]`` — join a room on a remote peer."""
        if not arg:
            self.notify("Usage: /j <room_id> [password]", severity="warning")
            return
        parts = arg.split(maxsplit=1)
        room_id = parts[0]
        password = parts[1] if len(parts) > 1 else None
        online = [p for p in api.list_online_peers() if p.peer_id != self.self_peer_id]
        if not online:
            self.notify("No online peer to ask", severity="warning")
            return
        # Try each online peer until one admits us. The room could be
        # hosted by any of them; asking a peer that doesn't have it
        # just returns ROOM_NOT_FOUND and we move on.
        for peer in online:
            result = api.join_remote_room(peer, room_id, self.self_peer_id, password)
            if result in ("JOINED", "ALREADY_MEMBER"):
                self.notify(f"Joined {room_id}" if result == "JOINED"
                            else "Already a member")
                self._refresh_rooms()
                return
            if result == "WRONG_PASSWORD":
                self.notify("Wrong password", severity="error")
                return
        self.notify("Room not found on any online peer", severity="error")

    def _handle_leave_command(self) -> None:
        """Leave the currently selected room. The 'default' room is
        protected — leaving it would delete the auto-chat room for
        everyone on this node (sole-admin cascade)."""
        room_id = self.selected_room_id
        if not room_id:
            return
        room = api.get_room(room_id)
        if room is None:
            return
        if room.name == "default":
            self.notify("Can't leave the default room", severity="warning")
            return
        if api.leave_room(room_id, self.self_peer_id):
            self.notify(f"Left #{room.name}")
            self.selected_room_id = None
            self._rendered_count = 0
            self.query_one("#message-log", RichLog).clear()
            self._refresh_rooms()
        else:
            self.notify("Not a member of this room", severity="warning")

    # ---- Actions (key bindings) ------------------------------------------

    def action_new_room(self) -> None:
        """Bound to ``n``."""

        def _on_dismiss(result: Optional[dict]) -> None:
            if result is None:
                return
            room = api.create_local_room(
                creator_id=self.self_peer_id,
                name=result["name"],
                is_public=result["is_public"],
                password=result["password"],
            )
            if room is None:
                self.notify("Could not create room (check name/creator)", severity="error")
                return
            self.selected_room_id = room.room_id
            self._refresh_rooms()
            self.query_one("#chat-title", Static).update(f"# {room.name}")
            self._refresh_messages(force=True)
            self.notify(f"Created #{room.name}")
            self.query_one("#input-box", Input).focus()

        self.push_screen(NewRoomModal(), _on_dismiss)

    def action_join_room_prompt(self) -> None:
        """Bound to ``j`` — pre-fills the input with the join command."""
        box = self.query_one("#input-box", Input)
        box.value = "/j "
        box.focus()

    def action_help(self) -> None:
        """Bound to ``?``."""
        self._handle_command("/help")


def main() -> int:
    import os
    peer_id = os.environ.get("LOCALLINK_NODE_ID", "").strip()
    app = LocalLinkApp(self_peer_id=peer_id)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
