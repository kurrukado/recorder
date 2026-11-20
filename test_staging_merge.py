import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import staging_queue  # noqa: E402
import supabase_sync  # noqa: E402


class StagingMergeTests(unittest.TestCase):
    def test_delete_recordings_from_supabase_chunks_by_50(self):
        filenames = [f"alice_part{i}.mp4" for i in range(60)]
        ok_res = MagicMock(status_code=204, text="")
        ok_res.__enter__.return_value = ok_res
        ok_res.__exit__.return_value = False

        with patch.object(supabase_sync, "SUPABASE_KEY", "test-key"), \
             patch("requests.delete", return_value=ok_res) as mock_del, \
             patch("supabase_sync.delete_thumbnails_from_supabase", return_value=True) as mock_del_thumbs:
            ok = supabase_sync.delete_recordings_from_supabase("alice", filenames)

        self.assertTrue(ok)
        self.assertEqual(mock_del.call_count, 2)
        first_url = mock_del.call_args_list[0][0][0]
        second_url = mock_del.call_args_list[1][0][0]
        self.assertIn("alice_part0.mp4", first_url)
        self.assertIn("alice_part49.mp4", first_url)
        self.assertNotIn("alice_part50.mp4", first_url)
        self.assertIn("alice_part50.mp4", second_url)
        self.assertIn("alice_part59.mp4", second_url)
        mock_del_thumbs.assert_called_once()

    def _run_package_with_mocks(self, manifest, *, sync_return=True, deleted_fids=None):
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        patches = [
            patch("staging_queue._get_user_staging_lock", return_value=mock_lock),
            patch("gdrive_manager.get_access_token", return_value="fake_tok"),
            patch("staging_queue.find_staging_folder", return_value=("st_u1", "st_root")),
            patch("staging_queue.get_staging_manifest", return_value=manifest),
            patch("gdrive_manager.download_file_from_drive", return_value=True),
            patch("staging_queue._normalize_segment_resolutions", side_effect=lambda files, _d: files),
            patch("recorder_core.concat_mp4_segments", side_effect=lambda _segs, out: out),
            patch("auto_h264.ensure_h264", side_effect=lambda p: p),
            patch("auto_h264.upscale_to_1080p_if_needed", side_effect=lambda p: p),
            patch("staging_queue.validate_playable_video", return_value=(True, "ok", 1200.0)),
            patch("auto_h264.extract_middle_thumbnail", return_value=None),
            patch("gdrive_manager.find_or_create_folder", return_value="main_fid"),
            patch("gdrive_manager.upload_file_to_drive", return_value="merged_drive_fid"),
            patch("supabase_sync.sync_recording_to_supabase", return_value=sync_return),
            patch("gdrive_manager.delete_file_drive", side_effect=(lambda fid, access_token=None: deleted_fids.append(fid)) if deleted_fids is not None else None),
            patch("supabase_sync.delete_recordings_from_supabase", return_value=True),
            patch("staging_queue._list_staging_mp4s", return_value=[]),
            patch("os.makedirs"),
            patch("os.path.getsize", return_value=500 * 1024),
            patch("os.path.exists", side_effect=lambda p: not str(p).endswith(".jpg")),
            patch("os.remove"),
            patch("shutil.rmtree"),
        ]
        with ExitStack() as stack:
            entered = [stack.enter_context(p) for p in patches]
            res = staging_queue.package_and_publish_queue("alice", access_token="fake_tok")
            return res, entered[14], entered[15]

    def test_force_merge_runs_immediately_and_preserves_old_files_when_supabase_fails(self):
        manifest = {
            "user": "alice",
            "segments": [
                {"filename": "alice_2026-11-20_12-00-00_full.mp4", "file_id": "fid_1", "duration": 600.0},
                {"filename": "alice_2026-11-20_12-00-00_part2.mp4", "file_id": "fid_2", "duration": 600.0},
            ],
            "last_live_time": int(time.time()),
            "force_merge": True,
            "manifest_file_id": "mfid_1",
        }

        folders_resp = MagicMock(status_code=200)
        folders_resp.json.return_value = {"files": [{"id": "st_u1", "name": "alice"}]}
        folders_resp.__enter__.return_value = folders_resp
        folders_resp.__exit__.return_value = False

        with patch("gdrive_manager.get_access_token", return_value="fake_tok"), \
             patch("gdrive_manager.find_or_create_folder", return_value="root_fid"), \
             patch("requests.get", return_value=folders_resp), \
             patch("staging_queue.get_staging_manifest", return_value=manifest), \
             patch("recorder_core.check_live_status") as mock_live, \
             patch("staging_queue.package_and_publish_queue", return_value={"ok": True}) as mock_pkg:
            flushed = staging_queue.check_and_flush_idle_queues(access_token="fake_tok")
            self.assertEqual(flushed, ["alice"])
            mock_live.assert_not_called()
            mock_pkg.assert_called_once_with("alice", access_token="fake_tok")

        # If Supabase sync fails, old files on Drive and DB MUST NOT be deleted
        fail_res, mock_drive_del_fail, mock_sb_del_fail = self._run_package_with_mocks(manifest, sync_return=False)
        self.assertFalse(fail_res.get("ok"))
        self.assertEqual(fail_res.get("filename"), "alice_2026-11-20_12-00-00_merged.mp4")
        mock_drive_del_fail.assert_not_called()
        mock_sb_del_fail.assert_not_called()

        # When Supabase sync succeeds, old Drive files and old DB rows are deleted cleanly
        deleted_fids = []
        ok_res, _, mock_sb_del_ok = self._run_package_with_mocks(manifest, sync_return=True, deleted_fids=deleted_fids)
        self.assertTrue(ok_res.get("ok"))
        self.assertEqual(ok_res.get("filename"), "alice_2026-11-20_12-00-00_merged.mp4")
        mock_sb_del_ok.assert_called_once()
        self.assertEqual(
            sorted(mock_sb_del_ok.call_args[0][1]),
            ["alice_2026-11-20_12-00-00_full.mp4", "alice_2026-11-20_12-00-00_part2.mp4"],
        )
        self.assertIn("fid_1", deleted_fids)
        self.assertIn("fid_2", deleted_fids)
        self.assertNotIn("merged_drive_fid", deleted_fids)


if __name__ == "__main__":
    unittest.main()
