import threading
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import cloud_daemon
import supabase_sync
import recorder_core
import auto_h264


class RecordingControlsTests(unittest.TestCase):
    def test_render_health_never_waits_for_cloud_dependencies(self):
        import api_server
        from fastapi.testclient import TestClient
        with patch.object(api_server.gdrive_manager, "load_active_recordings_from_drive") as drive, \
             patch.object(api_server.supabase_sync, "get_active_recordings_from_supabase") as database, \
             patch.dict(api_server.ACTIVE_RECORDING_TASKS, {"local_test": {}}, clear=True):
            client = TestClient(api_server.app)
            for path in ("/health", "/api/health"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["active_recordings"], ["local_test"])
            drive.assert_not_called()
            database.assert_not_called()

    def test_status_still_includes_cloud_recorders(self):
        import api_server
        from fastapi.testclient import TestClient
        with patch.object(api_server.gdrive_manager, "load_active_recordings_from_drive", return_value=["drive_test"]), \
             patch.object(api_server.supabase_sync, "get_active_recordings_from_supabase", return_value=["db_test"]), \
             patch.dict(api_server.ACTIVE_RECORDING_TASKS, {}, clear=True):
            response = TestClient(api_server.app).get("/api/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()["active_recordings"]), {"drive_test", "db_test"})

    def test_preferences_keep_disabled_streamers(self):
        response = MagicMock(status_code=200)
        response.json.return_value = [
            {"username": "@Alice", "recording_enabled": False},
            {"username": "bob", "recording_enabled": True},
        ]
        response.__enter__.return_value = response
        with patch.object(supabase_sync, "SUPABASE_KEY", "test"), \
             patch.object(supabase_sync.requests, "get", return_value=response):
            self.assertEqual(supabase_sync.fetch_recording_preferences(), {"alice": False, "bob": True})

    def test_pause_stops_softly_without_deleting_or_freeing_slot(self):
        stop = threading.Event()
        entry = {"stop_event": stop, "thread": MagicMock()}
        with patch.object(cloud_daemon, "_PAUSED_USERS", set()), \
             patch.dict(cloud_daemon.ACTIVE_RECORDERS, {"alice": entry}, clear=True):
            cloud_daemon.apply_recording_preferences({"alice": False})
            self.assertTrue(stop.is_set())
            self.assertFalse(getattr(stop, "user_deleted", False))
            self.assertIn("alice", cloud_daemon.ACTIVE_RECORDERS)
            self.assertIn("alice", cloud_daemon._PAUSED_USERS)

    def test_resume_clears_backoff_and_outage_preserves_pause(self):
        with patch.object(cloud_daemon, "_PAUSED_USERS", {"alice"}), \
             patch.dict(cloud_daemon._USER_START_COOLDOWN, {"alice": 999}, clear=True), \
             patch.dict(cloud_daemon._USER_FAILURE_STREAK, {"alice": 5}, clear=True), \
             patch.object(cloud_daemon, "_SESSION_SKIP_USERS", {"alice"}):
            cloud_daemon.apply_recording_preferences(None)
            self.assertIn("alice", cloud_daemon._PAUSED_USERS)
            cloud_daemon.apply_recording_preferences({"alice": True})
            self.assertNotIn("alice", cloud_daemon._PAUSED_USERS)
            self.assertNotIn("alice", cloud_daemon._USER_START_COOLDOWN)
            self.assertNotIn("alice", cloud_daemon._SESSION_SKIP_USERS)

    def test_manual_start_rejects_paused_streamer(self):
        import api_server
        from fastapi import HTTPException
        with patch.object(supabase_sync, "fetch_recording_preferences", return_value={"alice": False}):
            with self.assertRaises(HTTPException) as raised:
                api_server.start_record(api_server.RecordRequest(username="alice"), None)
            self.assertEqual(raised.exception.status_code, 409)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_hard_kill_capture_can_be_remuxed_and_decoded(self):
        captured = []
        proc = MagicMock()
        proc.poll.return_value = 0
        with patch.object(recorder_core.subprocess, "Popen", side_effect=lambda cmd, **kw: captured.extend(cmd) or proc), \
             patch.object(recorder_core, "FFMPEG_PATH", "ffmpeg"), \
             patch.object(recorder_core.shutil, "which", return_value="ffmpeg"), \
             patch.object(recorder_core.os.path, "exists", return_value=False), \
             patch.object(recorder_core.os, "makedirs"):
            recorder_core.record_stream_ffmpeg("http://example.invalid/live.flv", output_filename="capture.mp4", auto_sync_gdrive=False)
        flags = captured[captured.index("-movflags"):captured.index("-movflags") + 4]
        self.assertIn("empty_moov", flags[1])
        with tempfile.TemporaryDirectory() as folder:
            capture = Path(folder) / "capture.mp4"
            cmd = [shutil.which("ffmpeg"), "-v", "error", "-re", "-f", "lavfi", "-i",
                   "testsrc2=size=320x568:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                   "-c:a", "aac", "-c:v", "libx264", "-preset", "ultrafast",
                   "-g", "25", *flags, str(capture)]
            worker = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                time.sleep(10)
                self.assertIsNone(worker.poll())
            finally:
                worker.kill()
                worker.communicate(timeout=10)
            self.assertGreater(capture.stat().st_size, 250000)
            self.assertFalse(auto_h264.has_faststart(str(capture)))
            with patch.object(auto_h264, "FFMPEG_PATH", shutil.which("ffmpeg")):
                normalized = auto_h264.ensure_h264(str(capture))
            self.assertTrue(auto_h264.has_faststart(normalized))
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-xerror", "-i", normalized,
                                     "-f", "null", "-"], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
