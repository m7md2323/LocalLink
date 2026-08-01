import os
import tempfile
import unittest

# Use a per-process temporary file rather than ":memory:" — SqliteQueueDatabase
# uses separate connections for the writer thread and main-thread reads, and
# each ":memory:" connection gets its own private database.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LOCALLINK_DB_PATH"] = _tmp_db.name

from engine.storage.connection import get_db
from engine.storage.models import Message, Peer, Room, RoomMember


class TestMessageModel(unittest.TestCase):

    def setUp(self):
        # Bind the shared DB and create tables before each test.
        db = get_db()
        db.connect(reuse_if_open=True)
        db.create_tables([Peer, Message, Room, RoomMember], safe=True)

    def tearDown(self):
        # Clean up tables between tests.
        db = get_db()
        db.drop_tables([Peer, Message, Room, RoomMember], safe=True)

    def test_message_creation_and_defaults(self):
        room = Room.create(name="test-room", creator_id="alice_node")
        msg = Message(sender_id="alice_node", content="Hello P2P Mesh!", room=room)
        self.assertEqual(msg.sender_id, "alice_node")
        self.assertEqual(msg.content, "Hello P2P Mesh!")
        self.assertEqual(msg.room_id, room.room_id)
        self.assertFalse(msg.is_synced)
        self.assertTrue(len(msg.message_id) > 0)

    def test_dict_serialization(self):
        room = Room.create(name="test-room", creator_id="alice_node")
        msg = Message(sender_id="alice_node", content="Test dict conversion", room=room)
        d = msg.to_dict()

        self.assertEqual(d["sender_id"], "alice_node")
        self.assertEqual(d["content"], "Test dict conversion")
        self.assertEqual(d["message_id"], msg.message_id)

        restored = Message.from_dict(d)
        self.assertEqual(restored.message_id, msg.message_id)
        self.assertEqual(restored.content, msg.content)



    def test_peewee_save_and_retrieve(self):
        room = Room.create(name="test-room", creator_id="charlie_node")
        msg = Message.create(sender_id="charlie_node", content="Persisted message", room=room)
        self.assertIsNotNone(msg.message_id)

        fetched = Message.get(Message.message_id == msg.message_id)
        self.assertEqual(fetched.sender_id, "charlie_node")
        self.assertEqual(fetched.content, "Persisted message")
        self.assertFalse(fetched.is_synced)

    def test_mark_synced(self):
        room = Room.create(name="test-room", creator_id="alice_node")
        msg = Message(sender_id="alice_node", content="Sync me", room=room)
        self.assertFalse(msg.is_synced)
        msg.mark_synced()
        self.assertTrue(msg.is_synced)


if __name__ == "__main__":
    unittest.main()
