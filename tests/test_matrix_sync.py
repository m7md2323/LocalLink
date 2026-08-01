"""Smoke tests for bridge/matrix_sync.py.

We don't hit a real Matrix homeserver here — the test stubs out the
HTTP layer with ``unittest.mock.patch`` and exercises the DB side
only. The point is to lock in the contract: a successful
``send_message`` call must result in ``is_synced=True`` on the local
row, and a failure must leave the row untouched.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LOCALLINK_DB_PATH"] = _tmp_db.name

from engine.storage import database
from engine.storage.models import Message, Peer, Room, RoomMember
from bridge import matrix_sync


class TestResolveMatrixRoomId(unittest.TestCase):

    def test_prefers_room_level_mapping(self):
        room = Room(name="x", creator_id="alice", matrix_room_id="!specific:matrix.org")
        self.assertEqual(
            matrix_sync._resolve_matrix_room_id(room, "!fallback:matrix.org"),
            "!specific:matrix.org",
        )

    def test_falls_back_to_default(self):
        room = Room(name="x", creator_id="alice", matrix_room_id=None)
        self.assertEqual(
            matrix_sync._resolve_matrix_room_id(room, "!fallback:matrix.org"),
            "!fallback:matrix.org",
        )

    def test_returns_none_when_unset(self):
        room = Room(name="x", creator_id="alice", matrix_room_id=None)
        self.assertIsNone(matrix_sync._resolve_matrix_room_id(room, None))


class TestSyncOnce(unittest.TestCase):

    def setUp(self):
        db = database.get_db()
        db.connect(reuse_if_open=True)
        db.create_tables([Peer, Message, Room, RoomMember], safe=True)
        Peer.create(
            peer_id="alice",
            name="Alice",
            ip_address="127.0.0.1",
            port=5000,
        )
        self.room = Room.create(
            name="general",
            creator_id="alice",
        )
        RoomMember.create(room=self.room, peer_id="alice")

    def tearDown(self):
        db = database.get_db()
        db.drop_tables([Peer, Message, Room, RoomMember], safe=True)

    def test_marks_synced_on_success(self):
        msg = database.save_message(
            room_id=self.room.room_id,
            sender_id="alice",
            content="hello matrix",
        )
        self.assertFalse(msg.is_synced)

        with patch(
            "bridge.matrix_sync.send_message",
            return_value="$evt123:matrix.org",
        ):
            pushed = matrix_sync.sync_once(
                homeserver_url="https://matrix.org",
                access_token="dummy",
                default_room_id="!fallback:matrix.org",
            )

        self.assertEqual(pushed, 1)
        reloaded = database.get_message(msg.message_id)
        self.assertTrue(reloaded.is_synced)
        self.assertEqual(reloaded.matrix_event_id, "$evt123:matrix.org")

    def test_leaves_unsynced_on_failure(self):
        msg = database.save_message(
            room_id=self.room.room_id,
            sender_id="alice",
            content="will fail",
        )

        with patch(
            "bridge.matrix_sync.send_message",
            return_value=None,
        ):
            pushed = matrix_sync.sync_once(
                homeserver_url="https://matrix.org",
                access_token="dummy",
                default_room_id="!fallback:matrix.org",
            )

        self.assertEqual(pushed, 0)
        reloaded = database.get_message(msg.message_id)
        self.assertFalse(reloaded.is_synced)
        self.assertIsNone(reloaded.matrix_event_id)

    def test_skips_message_when_no_room_mapping(self):
        msg = database.save_message(
            room_id=self.room.room_id,
            sender_id="alice",
            content="orphan",
        )

        with patch(
            "bridge.matrix_sync.send_message",
            return_value="$evt:matrix.org",
        ) as mocked:
            pushed = matrix_sync.sync_once(
                homeserver_url="https://matrix.org",
                access_token="dummy",
                default_room_id=None,
            )

        self.assertEqual(pushed, 0)
        mocked.assert_not_called()
        reloaded = database.get_message(msg.message_id)
        self.assertFalse(reloaded.is_synced)

    def test_uses_per_room_mapping_over_default(self):
        other_room = database.create_room(
            creator_id="alice",
            name="other",
            is_public=True,
        )
        database.join_room(other_room.room_id, "alice")
        other_room.matrix_room_id = "!other:matrix.org"
        other_room.save()

        msg = database.save_message(
            room_id=other_room.room_id,
            sender_id="alice",
            content="routed correctly",
        )

        captured = {}
        def fake_send(homeserver_url, access_token, matrix_room_id, body, txn_id, sender_display=None):
            captured["matrix_room_id"] = matrix_room_id
            return "$evt:matrix.org"

        with patch("bridge.matrix_sync.send_message", side_effect=fake_send):
            matrix_sync.sync_once(
                homeserver_url="https://matrix.org",
                access_token="dummy",
                default_room_id="!default:matrix.org",
            )

        self.assertEqual(captured["matrix_room_id"], "!other:matrix.org")
        self.assertTrue(database.get_message(msg.message_id).is_synced)


if __name__ == "__main__":
    unittest.main()
