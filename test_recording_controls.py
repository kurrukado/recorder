import threading
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import cloud_daemon
import supabase_sync
import recorder_core
import staging_queue
import auto_h264


class RecordingControlsTests(unittest.TestCase):
    def test_legacy_capture_repairs_nonempty_rejected_media_before_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "capture.mp4"
            proc = MagicMock()
            proc.poll.return_value = 0

            def start(cmd, **_options):
                Path(cmd[-1]).write_bytes(b"raw" * 100_000)
                return proc

            def normalize(candidate):
                self.assertNotEqual(Path(candidate), video)
                self.assertEqual(video.read_bytes(), b"raw" * 100_000)
                Path(candidate).write_bytes(b"mp4" * 100_000)
                return candidate

            with patch.object(recorder_core.subprocess, "Popen", side_effect=start), \
                    patch.object(recorder_core.shutil, "which", return_value="ffmpeg"), \
                    patch.object(auto_h264, "validate_playable_video", side_effect=[(False, "bad tail", 8), (True, "ok", 8), (True, "ok", 8)]), \
                    patch.object(auto_h264, "ensure_h264", side_effect=normalize), \
                    patch.object(auto_h264, "upscale_to_1080p_if_needed", side_effect=lambda path: path):
                result = recorder_core.record_stream_ffmpeg("https://fixture.invalid/source.flv", str(video), duration=10, auto_sync_gdrive=False)
            self.assertEqual(result, str(video))
            self.assertEqual(video.read_bytes(), b"mp4" * 100_000)

    def test_legacy_capture_keeps_original_even_when_repair_fails_and_cleans_header_only_output(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "capture.mp4"
            proc = MagicMock()
            proc.poll.return_value = 0

            def normalize(candidate):
                Path(candidate).write_bytes(b"bad repair")
                return candidate

            for size in (300_000, 1257):
                def start(cmd, **_options):
                    Path(cmd[-1]).write_bytes(b"x" * size)
                    return proc

                with patch.object(recorder_core.subprocess, "Popen", side_effect=start), \
                        patch.object(recorder_core.shutil, "which", return_value="ffmpeg"), \
                        patch.object(auto_h264, "validate_playable_video", return_value=(False, "bad file", 0)), \
                        patch.object(auto_h264, "ensure_h264", side_effect=normalize) as repair:
                    result = recorder_core.record_stream_ffmpeg("https://fixture.invalid/source.flv", str(video), duration=10, auto_sync_gdrive=False)
                self.assertIsNone(result)
                self.assertFalse(video.exists())
                self.assertEqual(repair.call_count, int(size >= 250_000))
                retained = list((Path(directory) / ".capture-recovery").glob("*/*.mp4"))
                self.assertEqual([path.read_bytes() for path in retained], [b"x" * 300_000])

    def test_control_only_disables_capture_and_keeps_direct_live_metadata(self):
        import api_server
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"RECORD_CONTROL_ONLY": "1", "RECORD_PIN": "fixture"}), \
                patch.object(api_server, "get_user_live_status_cached", return_value=(True, "room")), \
                patch.object(api_server.recorder_core, "get_live_stream_url", return_value="https://media.invalid/live.m3u8"), \
                patch.object(api_server, "bg_record_worker") as capture:
            client = TestClient(api_server.app)
            self.assertEqual(client.post("/api/record/start", json={"username": "alice"}).status_code, 409)
            response = client.get("/api/stream/alice")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["stream_url"], "https://media.invalid/live.m3u8")
            capture.assert_not_called()

    def test_control_only_blocks_deletes_and_fails_closed_without_pin(self):
        import api_server
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"RECORD_CONTROL_ONLY": "1", "RECORD_PIN": "fixture"}, clear=False):
            client = TestClient(api_server.app)
            self.assertEqual(client.delete("/api/users/alice", headers={"x-record-pin": "fixture"}).status_code, 409)
            self.assertEqual(client.delete("/api/recordings", headers={"x-record-pin": "fixture"}).status_code, 409)

        with patch.dict(os.environ, {"RECORD_CONTROL_ONLY": "1", "RECORD_PIN": ""}, clear=False), \
                patch.object(api_server, "get_api_pin", return_value=""):
            client = TestClient(api_server.app)
            response = client.put("/api/users/alice", json={"recording_enabled": False})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["status"], "pin_required")

    def test_render_environment_is_guarded_without_control_flag(self):
        import api_server
        with patch.dict(os.environ, {"RENDER": "true", "RECORD_CONTROL_ONLY": ""}, clear=False):
            self.assertTrue(api_server.guarded_runtime())
            from fastapi.testclient import TestClient
            client = TestClient(api_server.app)
            self.assertEqual(client.delete("/api/recordings").status_code, 409)
        with patch.dict(os.environ, {"RENDER": "", "RECORD_CONTROL_ONLY": ""}, clear=False):
            self.assertFalse(api_server.guarded_runtime())

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

    def test_live_scan_starts_newest_eligible_streamer_while_tail_is_pending(self):
        older_probe_done = threading.Event()
        recording_started = threading.Event()
        scan_finished = threading.Event()
        recorded_users = []
        tail_saw_recording = []

        def check_live(user):
            if user == "slow_offline":
                tail_saw_recording.append(recording_started.wait(timeout=2))
                return {"is_live": False}
            if user == "newest":
                older_probe_done.wait(timeout=5)
            return {"is_live": True, "room_id": user}

        def record(user, room_id, auto_discover, stop_event):
            recorded_users.append(user)
            recording_started.set()
            stop_event.wait(timeout=5)

        def finish_scan():
            scan_finished.set()
            return True

        # Wait for the older live result to be ready before returning the newest.
        submit = ThreadPoolExecutor.submit

        def submit_probe(executor, fn, user):
            future = submit(executor, fn, user)
            if user == "older":
                future.add_done_callback(lambda _: older_probe_done.set())
            return future

        clock = SimpleNamespace(time=lambda: 1000 if scan_finished.is_set() else 1, sleep=lambda _: None)
        users = ["newest", "older", "slow_offline"]
        with patch.object(ThreadPoolExecutor, "submit", autospec=True, side_effect=submit_probe), \
             patch.object(cloud_daemon, "load_monitored_users", return_value=users), \
             patch.object(cloud_daemon, "_fast_poll_new_streamers", side_effect=finish_scan) as poll, \
             patch.object(cloud_daemon, "_RECORDING_PREFERENCES_READY", True), \
             patch.object(cloud_daemon, "MAX_CONCURRENT_RECORDERS", 1), \
             patch.dict(cloud_daemon.ACTIVE_RECORDERS, {}, clear=True), \
             patch.dict(cloud_daemon._USER_START_COOLDOWN, {}, clear=True), \
             patch.object(cloud_daemon, "time", clock), \
             patch.object(cloud_daemon, "log"), \
             patch.object(cloud_daemon, "streamer_recording_worker", side_effect=record), \
             patch.object(recorder_core, "check_live_details", side_effect=check_live), \
             patch.object(cloud_daemon.gdrive_manager, "get_access_token", return_value=None), \
             patch.object(cloud_daemon.gdrive_manager, "load_active_recordings_from_drive", return_value=[]), \
             patch.object(cloud_daemon.gdrive_manager, "set_user_recording_status_drive"), \
             patch.object(supabase_sync, "fetch_streamers_details_from_supabase", return_value=[]), \
             patch.object(supabase_sync, "update_streamer_status_supabase") as status, \
             patch.object(cloud_daemon.notifier, "send_telegram"), \
             patch.object(staging_queue, "check_and_flush_idle_queues"):
            cloud_daemon.run_daemon(max_minutes=1)

        self.assertEqual(recorded_users, ["newest"])
        self.assertEqual(tail_saw_recording, [True])
        status.assert_any_call("older", is_live=True, is_recording=False, room_id="older",
                               nickname=None, avatar_thumb=None, status="live")
        poll.assert_called_once_with()

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
             patch.object(recorder_core.os.path, "isfile", return_value=False), \
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
