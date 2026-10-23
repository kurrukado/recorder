import unittest
from unittest.mock import patch, MagicMock
import threading
import time
import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestAdversarialDeletion(unittest.TestCase):

    def test_extract_tiktok_username_edge_cases(self):
        """Test username normalization with multiple @, mixed case, URLs, and edge symbols."""
        from api_server import extract_tiktok_username

        cases = [
            ("user1", "user1"),
            ("@user1", "user1"),
            ("@@user1", "user1"),
            ("@@@@@user1", "user1"),
            ("  @@@User_ABC  ", "user_abc"),
            ("Mixed_Case_User", "mixed_case_user"),
            ("@Mixed_Case_User", "mixed_case_user"),
            ("@@@MIXED_case_999", "mixed_case_999"),
            ("streamer.name_123", "streamer.name_123"),
            ("@@@streamer.name_123", "streamer.name_123"),
            ("https://www.tiktok.com/@my_streamer", "my_streamer"),
            ("https://www.tiktok.com/@@@my_streamer", "my_streamer"),
            ("https://www.tiktok.com/@MY_STREAMER?is_from_webapp=1", "my_streamer"),
            ("https://www.tiktok.com/@my_streamer/live", "my_streamer"),
            ("", ""),
            (None, ""),
        ]
        for inp, expected in cases:
            actual = extract_tiktok_username(inp)
            self.assertEqual(actual, expected, f"Failed for input: {inp}")

    def test_delete_non_existent_streamer_is_idempotent(self):
        """Deleting a non-existent streamer must be idempotent and return ok: True without errors."""
        import api_server

        with patch("api_server.load_config", return_value={"monitored_users": ["active_user_1"]}), \
             patch("api_server.save_config") as mock_save, \
             patch("supabase_sync.delete_streamer_data_supabase") as mock_sb_del, \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["active_user_1"]), \
             patch("gdrive_manager.delete_streamer_folder_drive"):

            # User 'ghost_user' is not in monitored_users or active tasks
            mock_bg = MagicMock()
            res = api_server.delete_user("@@@ghost_user", delete_files=True, bg_tasks=mock_bg)
            self.assertTrue(res.get("ok"))
            # Config was not modified since user was not in it
            mock_save.assert_not_called()
            # Supabase delete was still called to ensure DB cleanup
            mock_sb_del.assert_called_with("ghost_user")

    @patch("api_server.load_config", return_value={"monitored_users": ["target_parallel"]})
    @patch("api_server.save_config")
    @patch("supabase_sync.delete_streamer_data_supabase")
    @patch("gdrive_manager.load_streamers_from_drive", return_value=[])
    @patch("gdrive_manager.delete_streamer_folder_drive")
    def test_rapid_consecutive_delete_calls_concurrency(self, mock_drive_del, mock_drive_load, mock_sb_del, mock_cfg_save, mock_cfg_load):
        """Simulate rapid consecutive delete calls in parallel threads."""
        import api_server

        results = []
        errors = []

        def worker_delete(username):
            try:
                mock_bg = MagicMock()
                r = api_server.delete_user(username, delete_files=False, bg_tasks=mock_bg)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker_delete, args=("@@@target_parallel",)),
            threading.Thread(target=worker_delete, args=("@TARGET_PARALLEL",)),
            threading.Thread(target=worker_delete, args=("target_parallel",)),
            threading.Thread(target=worker_delete, args=("@@TARGET_parallel",)),
            threading.Thread(target=worker_delete, args=("TARGET_PARALLEL",)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent deletes raised errors: {errors}")
        self.assertEqual(len(results), 5)
        for r in results:
            self.assertTrue(r.get("ok"))

    def test_active_worker_resurrection_immunity_when_deleted(self):
        """Ensure active recording thread terminates cleanly and skips all uploads and DB re-upserts."""
        import cloud_daemon

        user = "victim_streamer"
        stop_ev = threading.Event()
        stop_ev.user_deleted = True
        stop_ev.set()

        with patch("cloud_daemon.load_monitored_users", return_value=[]), \
             patch("recorder_core.check_live_details", return_value={"is_live": False, "room_id": None}), \
             patch("gdrive_manager.set_user_recording_status_drive") as mock_set_rec, \
             patch("gdrive_manager.upload_file_to_drive") as mock_drive_upload, \
             patch("supabase_sync.sync_recording_to_supabase") as mock_supa_sync, \
             patch("supabase_sync.update_streamer_status_supabase") as mock_status_sync, \
             patch("shutil.rmtree"):

            cloud_daemon.streamer_recording_worker(user, "room_123", stop_event=stop_ev)

            # Assert no resurrection happened:
            mock_drive_upload.assert_not_called()
            mock_supa_sync.assert_not_called()
            # update_streamer_status_supabase must NOT be called to set offline (which would re-insert row in Supabase)
            # when stop_event.user_deleted is True
            for call in mock_status_sync.call_args_list:
                args, kwargs = call
                self.assertNotEqual(kwargs.get("status"), "offline", "Worker resurrected deleted user by calling update_streamer_status_supabase(status='offline')!")

    def test_cloud_daemon_drive_pruning_with_mixed_case(self):
        """When Drive has mixed-case or @ prefixed usernames of deleted streamers, daemon normalizes and prunes them."""
        import cloud_daemon

        cloud_daemon._CACHED_DRIVE_USERS = None
        cloud_daemon._LAST_DRIVE_CHECK = 0

        with patch("supabase_sync.fetch_streamers_from_supabase", return_value=["valid_streamer"]), \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["valid_streamer", "@@@DELETED_1", "@Deleted_2"]), \
             patch("gdrive_manager.save_streamers_to_drive") as mock_save, \
             patch("cloud_daemon.load_config", return_value={"monitored_users": []}):

            users = cloud_daemon.load_monitored_users()
            self.assertEqual(users, ["valid_streamer"])
            self.assertTrue(mock_save.called)
            # Check what was saved to Drive:
            saved_users = mock_save.call_args[0][0]
            self.assertEqual(saved_users, ["valid_streamer"])

    def test_supabase_thumbnail_cleanup_pagination(self):
        """Test that delete_streamer_data_supabase paginates until all thumbnails are deleted using two-phase cleanup."""
        import supabase_sync

        total_items = 150
        all_objects = [{"name": f"thumb_{i:04d}.jpg"} for i in range(total_items)]
        list_calls = []
        delete_calls = []
        deleted_prefixes = []

        class MockResponse:
            def __init__(self, status_code, json_data):
                self.status_code = status_code
                self._json = json_data
                self.text = "OK"

            def json(self):
                return self._json

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_post(url, *args, **kwargs):
            if "object/list" in url:
                payload = kwargs.get("json", {})
                offset = payload.get("offset", 0)
                limit = payload.get("limit", 100)
                list_calls.append({"offset": offset, "limit": limit})
                # Phase 1: Slice from stored objects by offset
                slice_data = all_objects[offset:offset + limit]
                return MockResponse(200, slice_data)
            return MockResponse(200, {})

        def mock_delete(url, *args, **kwargs):
            delete_calls.append((url, kwargs.get("json", {})))
            if "storage/v1/object" in url:
                prefixes = kwargs.get("json", {}).get("prefixes", [])
                deleted_prefixes.extend(prefixes)
            return MockResponse(200, {})

        with patch("requests.post", side_effect=mock_post), \
             patch("requests.delete", side_effect=mock_delete):

            ok = supabase_sync.delete_streamer_data_supabase("test_user_pagination")
            self.assertTrue(ok)

            # Verify Phase 1: Read-only listing paginated with offset
            self.assertGreaterEqual(len(list_calls), 2)
            self.assertEqual(list_calls[0]["offset"], 0)
            self.assertEqual(list_calls[1]["offset"], 100)

            # Verify Phase 2: Batch delete in chunks of 100
            storage_deletes = [c for c in delete_calls if "object/" in c[0]]
            self.assertEqual(len(storage_deletes), 2)
            self.assertEqual(len(storage_deletes[0][1]["prefixes"]), 100)
            self.assertEqual(len(storage_deletes[1][1]["prefixes"]), 50)
            self.assertEqual(len(deleted_prefixes), total_items)


if __name__ == "__main__":
    unittest.main()
