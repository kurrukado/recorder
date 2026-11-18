import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

import record_worker_media as media


class SpoolTests(unittest.TestCase):
    def test_drive_lane_starts_with_no_r2_credentials(self):
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": "catalog-fixture"}, clear=True):
            backend = media.MediaBackend()
        self.assertEqual(backend.auth_key, "catalog-fixture")
        self.assertFalse(backend.r2_enabled)

    def test_opaque_supabase_key_is_sent_as_apikey(self):
        backend = media.MediaBackend.__new__(media.MediaBackend)
        backend.auth_key = "sb_secret_fixture"
        backend.rest = "https://fixture.invalid/rest/v1"
        reply = MagicMock()
        with patch.object(media.requests, "request", return_value=reply) as request:
            backend.request("GET", "table")
        self.assertEqual(request.call_args.kwargs["headers"], {"apikey": "sb_secret_fixture"})

    def test_archive_rejects_drive_when_uploaded_checksum_differs(self):
        backend = media.MediaBackend.__new__(media.MediaBackend)
        reply = MagicMock()
        reply.__enter__.return_value.json.return_value = {
            "name": "sample.mp4", "size": "5", "mimeType": "video/mp4", "md5Checksum": "different",
        }
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(media.gdrive_manager, "get_access_token", return_value="drive-token"), \
                patch.object(media.gdrive_manager, "create_streamer_folder_drive", return_value=(True, "folder")), \
                patch.object(media.gdrive_manager, "upload_file_to_drive", return_value="driveId"), \
                patch.object(media.requests, "get", return_value=reply):
            output = Path(directory) / "sample.mp4"
            output.write_bytes(b"video")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                backend.archive("creator", output)

    def test_enqueue_reuses_the_job_even_after_publish_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            args = dict(username="creator", session_id="run_001", part_number=1, segments=[segment])
            manifest = media.enqueue_recording(spool, **args)
            job = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(job["segments"], ["chunk.ts"])
            self.assertEqual(segment.read_bytes(), b"capture")
            segment.unlink()
            self.assertEqual(media.enqueue_recording(spool, **args), manifest)
            with self.assertRaises(ValueError):
                media.enqueue_recording(spool, **(args | {"part_number": 2, "segments": [spool.parent / "other.ts"]}))

    def test_publish_checkpoints_drive_and_does_not_wait_for_r2(self):
        class Backend:
            def __init__(self):
                self.archives = 0
                self.writes = 0
                self.drive_fails = True
                self.catalog_fails = True

            def is_monitored(self, username):
                return username == "creator"

            def archive(self, username, output):
                self.archives += 1
                if self.drive_fails:
                    raise IOError("Drive unreachable")
                return "drive1"

            def cache(self, filename, drive_id, source):
                raise AssertionError("Archive publication must not access R2")

            def publish(self, job, output):
                self.writes += 1
                if self.catalog_fails:
                    raise IOError("Catalog unavailable")
                self.published_drive_id = job["drive_file_id"]
                return True

        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            args = dict(username="creator", session_id="run_001", part_number=1, segments=[segment])
            manifest = media.enqueue_recording(spool, **args)
            job = json.loads(manifest.read_text(encoding="utf-8"))
            output = manifest.parent / job["filename"]
            output.write_bytes(b"video")
            backend = Backend()
            with self.assertRaisesRegex(OSError, "Drive unreachable"):
                media._publish_job(spool, manifest, job, backend)
            self.assertEqual(backend.writes, 0)
            self.assertTrue(segment.is_file())
            backend.drive_fails = False
            with self.assertRaisesRegex(OSError, "Catalog unavailable"):
                media._publish_job(spool, manifest, job, backend)
            self.assertTrue(segment.is_file())
            self.assertTrue(output.is_file())
            job = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(job["drive_file_id"], "drive1")
            backend.catalog_fails = False
            media._publish_job(spool, manifest, job, backend)
            self.assertEqual(backend.archives, 2)
            self.assertEqual(backend.published_drive_id, "drive1")
            self.assertFalse(segment.exists())
            self.assertTrue(output.exists())
            self.assertTrue(media.enqueue_recording(spool, **args).is_file())

    def test_restore_uses_matching_local_mp4_and_preserves_it_when_r2_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            manifest = media.enqueue_recording(spool, username="creator", session_id="session", part_number=1,
                                               segments=[segment])
            job = json.loads(manifest.read_text(encoding="utf-8"))
            output = manifest.parent / job["filename"]
            output.write_bytes(b"valid finalized asset fixture")
            with patch.object(media.MediaBackend, "is_monitored", return_value=True), \
                    patch.object(media.MediaBackend, "archive", return_value="drive1"), \
                    patch.object(media.MediaBackend, "publish", return_value=True):
                backend = media.MediaBackend.__new__(media.MediaBackend)
                media._publish_job(spool, manifest, job, backend)
            restore = {"filename": job["filename"], "username": "creator", "drive_file_id": "drive1",
                       "job_key": f"restore:{job['filename']}"}
            catalog = [{"username": "creator", "drive_file_id": "drive1", "size_bytes": output.stat().st_size}]
            with patch.object(backend, "request", return_value=catalog), \
                    patch.object(backend, "renew_restore", return_value=True), \
                    patch.object(backend, "cache", side_effect=IOError("R2 unavailable")) as cached, \
                    patch.object(backend, "set_ready") as ready, \
                    patch.object(media.gdrive_manager, "get_access_token", side_effect=AssertionError("must use local MP4")):
                with self.assertRaisesRegex(OSError, "R2 unavailable"):
                    backend.restore(spool, restore, "owner")
                self.assertTrue(output.is_file())
                self.assertFalse(segment.exists())
                cached.assert_called_once_with(job["filename"], "drive1", output)
                ready.assert_not_called()

    def test_resumed_publish_preserves_sources_if_confirmed_mp4_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            manifest = media.enqueue_recording(spool, username="creator", session_id="session", part_number=1,
                                               segments=[segment])
            job = json.loads(manifest.read_text(encoding="utf-8"))
            output = manifest.parent / job["filename"]
            output.write_bytes(b"validated MP4")
            backend = MagicMock()
            backend.archive.return_value = "drive1"
            backend.publish.side_effect = OSError("Catalog offline")
            with self.assertRaises(OSError):
                media._publish_job(spool, manifest, job, backend)
            output.write_bytes(b"modified")
            job = json.loads(manifest.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "confirmed Drive archive"):
                media._publish_job(spool, manifest, job, backend)
            backend.publish.assert_called_once()
            self.assertTrue(segment.is_file())

    def test_cache_completion_during_publish_is_carried_into_archive_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            manifest = media.enqueue_recording(spool, username="creator", session_id="session", part_number=1,
                                               segments=[segment])
            job = json.loads(manifest.read_text(encoding="utf-8"))
            job["state"] = "finalized"
            output = manifest.parent / job["filename"]
            output.write_bytes(b"validated MP4")
            backend = MagicMock()
            backend.archive.return_value = "drive1"
            receipt = spool / "publish-done" / f"{manifest.parent.name}.json"

            def fast_restore(confirmed, _output):
                restore = {
                    "filename": confirmed["filename"], "drive_file_id": confirmed["drive_file_id"],
                    "enqueued_at": media._now().isoformat(),
                }
                media._record_cache_ready(spool, restore)
                self.assertFalse(receipt.exists())
                return True

            backend.publish.side_effect = fast_restore
            with self.assertLogs(media.LOG, level="INFO") as logs:
                media._publish_job(spool, manifest, job, backend)
            self.assertTrue(any("archive_to_ready_seconds=" in line for line in logs.output))
            self.assertIn("cache_ready_at", json.loads(receipt.read_text(encoding="utf-8")))
            media._maintain_local_cache(spool, [(manifest, job)])
            self.assertFalse(output.exists())

    def test_cache_pressure_does_not_evict_a_file_held_by_a_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            manifest = media.enqueue_recording(spool, username="creator", session_id="session", part_number=1,
                                               segments=[segment])
            job = json.loads(manifest.read_text(encoding="utf-8"))
            output = manifest.parent / job["filename"]
            output.write_bytes(b"validated MP4")
            backend = MagicMock()
            backend.archive.return_value = "drive1"
            media._publish_job(spool, manifest, job, backend)
            with media._borrow_local_mp4(spool, job["filename"], "creator", "drive1", output.stat().st_size) as held:
                self.assertEqual(held, output)
                media._maintain_local_cache(spool, [(manifest, job)], evict_all=True)
                self.assertTrue(output.exists())
            media._maintain_local_cache(spool, [(manifest, job)], evict_all=True)
            self.assertFalse(output.exists())
            self.assertTrue((spool / "publish-done" / f"{manifest.parent.name}.json").is_file())

    def test_finalize_defers_when_scratch_does_not_fit_without_using_a_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory)
            segment = spool / "chunk.ts"
            segment.write_bytes(b"capture")
            manifest = media.enqueue_recording(spool, username="creator", session_id="session", part_number=1,
                                               segments=[segment])
            job = json.loads(manifest.read_text(encoding="utf-8"))
            with patch.object(media.shutil, "disk_usage", return_value=MagicMock(free=0)), \
                    patch.object(media, "finalize_recording", side_effect=AssertionError("should wait for scratch")):
                media._run_local_job(lambda: media._finalize_job(spool, manifest, job), manifest, job)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["attempts"], 0)
            self.assertTrue(segment.is_file())

    def test_restore_refuses_a_drive_id_that_disagrees_with_the_catalog(self):
        backend = media.MediaBackend.__new__(media.MediaBackend)
        job = {"filename": "stream.mp4", "username": "creator", "drive_file_id": "foreign", "job_key": "restore:foreign"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(backend, "request", return_value=[{"username": "creator", "drive_file_id": "legit"}]), \
                patch.object(media.gdrive_manager, "get_access_token", side_effect=AssertionError("must not access Drive")):
            with self.assertRaisesRegex(ValueError, "catalog"):
                backend.restore(Path(directory), job, "lease")

    def test_transient_r2_outage_remains_queued_after_many_attempts(self):
        backend = MagicMock()
        backend.request.return_value = [{"filename": "sample.mp4", "attempts": media.MAX_ATTEMPTS + 2}]
        backend.restore.side_effect = RuntimeError("R2 timed out")
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(media._restore_next(Path(directory), backend))
        changes = backend.update_restore.call_args.args[2]
        self.assertEqual(changes["status"], "queued")
        self.assertEqual(changes["last_error"], "R2 timed out")

    def test_restore_validates_archive_bytes_and_leases_before_caching(self):
        backend = media.MediaBackend.__new__(media.MediaBackend)
        payload = b"archived video"
        job = {"filename": "stream.mp4", "username": "creator", "drive_file_id": "drive1", "job_key": "restore:drive1"}
        drive_meta = MagicMock()
        drive_meta.__enter__.return_value.json.return_value = {
            "name": "stream.mp4", "size": str(len(payload)), "mimeType": "video/mp4", "trashed": False,
            "capabilities": {"canDownload": True}, "md5Checksum": hashlib.md5(payload).hexdigest(),
        }
        received = []

        def download(_drive_id, dest, access_token):
            Path(dest).write_bytes(payload)
            return True

        def cache(_filename, _drive_id, source):
            received.append(source.read_bytes())
            return "recordings/r2.mp4", media._now()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(backend, "request", return_value=[{"username": "creator", "drive_file_id": "drive1"}]), \
                patch.object(backend, "renew_restore", side_effect=[True, False]), \
                patch.object(backend, "cached_until", return_value=None), patch.object(backend, "cache", side_effect=cache), \
                patch.object(backend, "set_ready", side_effect=AssertionError("lease was lost")), \
                patch.object(media.gdrive_manager, "get_access_token", return_value="token"), \
                patch.object(media.gdrive_manager, "is_recorder_owned_file", return_value=True), \
                patch.object(media.gdrive_manager, "download_file_from_drive", side_effect=download), \
                patch.object(media.requests, "get", return_value=drive_meta):
            spool = Path(directory)
            (spool / "restore").mkdir()
            with self.assertRaises(media.LeaseLost):
                backend.restore(spool, job, "lease")
            self.assertEqual(received, [payload])
            self.assertEqual(list((spool / "restore").iterdir()), [])

            drive_meta.__enter__.return_value.json.return_value["md5Checksum"] = "wrong"
            with self.assertRaisesRegex(ValueError, "checksum"):
                backend.restore(spool, job, "lease")
            self.assertEqual(received, [payload])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg and ffprobe")
class FinalizeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)

    def make_video(self, name, size="160x96", fps=12, audio=True, *, aspect="1", duration=2):
        target = self.path / name
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={size}:rate={fps}:duration={duration}", "-vf", f"setsar={aspect}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", "-g", str(round(Fraction(fps)))]
        if audio:
            cmd[8:8] = ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"]
            cmd += ["-c:a", "aac"]
        subprocess.run([*cmd, str(target)], check=True, capture_output=True)
        return target

    def test_compatible_mpegts_uses_copy(self):
        segments = [self.make_video(f"clip{i}.ts", audio=False) for i in range(2)]
        output = self.path / "recording.mp4"
        with patch.object(media, "_normalize", side_effect=AssertionError("compatible segments should copy")):
            self.assertGreater(media.finalize_recording(segments, output), 3.8)
        self.assertTrue(all(segment.exists() for segment in segments))
        self.assertLess(output.read_bytes().find(b"moov"), output.read_bytes().find(b"mdat"))

    def test_known_reconnects_sample_boundary_packets(self):
        segments = [self.make_video(f"clip{i}.ts", audio=False) for i in range(4)]
        output = self.path / "recording.mp4"
        with patch.object(media, "_validate_packets", wraps=media._validate_packets) as packets:
            media.finalize_recording(segments, output, reconnect_indexes=[2])
        self.assertEqual(packets.call_count, 3)
        self.assertTrue(all(call.kwargs["interval"] is not None for call in packets.call_args_list))

    def test_native_1080p_preserves_2997_and_60_fps_without_encoding(self):
        for index, (fps, frames) in enumerate([("30000/1001", "12"), ("60/1", "24")]):
            with self.subTest(fps=fps):
                segment = self.make_video(f"source{index}.ts", "1920x1080", fps=fps, audio=False, duration=0.4)
                output = self.path / f"recording{index}.mp4"
                with patch.object(media, "_normalize", side_effect=AssertionError("1080p native source should copy")):
                    media.finalize_recording([segment], output, reconnect_indexes=[])
                video = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-count_frames",
                    "-of", "json", str(output),
                ]))["streams"][0]
                self.assertEqual((video["width"], video["height"], video["r_frame_rate"], video["nb_read_frames"]),
                                 (1920, 1080, fps, frames))

    def test_hevc_is_normalized_to_browser_compatible_h264(self):
        if b"libx265" not in subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"]):
            self.skipTest("requires the FFmpeg libx265 encoder")
        source = self.path / "hevc.ts"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=24:duration=0.6",
            "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1:log-level=error", "-pix_fmt", "yuv420p",
            str(source),
        ], check=True, capture_output=True)
        output = self.path / "recording.mp4"
        self.assertEqual(media._probe(source)[0]["codec_name"], "hevc")
        media.finalize_recording([source], output, reconnect_indexes=[])
        video, audio, duration = media._probe(output)
        self.assertEqual((video["codec_name"], video["width"], video["height"]), ("h264", 160, 96))
        self.assertEqual(audio["codec_name"], "aac")
        self.assertGreater(duration, 0.5)

    def test_incompatible_reconnect_preserves_variable_frame_timing(self):
        segments = [self.make_video("low.ts"), self.make_video("high.ts", "192x144", fps=24, audio=False)]
        output = self.path / "recording.mp4"
        self.assertGreater(media.finalize_recording(segments, output), 3.8)
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-count_frames", "-of", "json", str(output),
        ]))
        video, audio = probe["streams"]
        self.assertEqual((video["codec_name"], video["width"], video["height"]), ("h264", 160, 96))
        self.assertEqual(audio["codec_name"], "aac")
        self.assertEqual(int(video["nb_read_frames"]), 72)
        self.assertTrue(all(segment.exists() for segment in segments))

    def test_normalization_preserves_frames_across_bounded_input_groups(self):
        segments = [self.make_video("first.ts"), self.make_video("second.ts", "192x144", fps=24)]
        output = self.path / "recording.mp4"
        with patch.object(media, "NORMALIZE_INPUTS", 1):
            self.assertGreater(media.finalize_recording(segments, output), 3.8)
        frames = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames",
            "-of", "default=noprint_wrappers=1:nokey=1", str(output),
        ])
        self.assertEqual(int(frames), 72)

    def test_bad_segment_never_replaces_output_or_deletes_original(self):
        bad = self.path / "broken.ts"
        bad.write_bytes(b"broken")
        output = self.path / "recording.mp4"
        output.write_bytes(b"previous")
        with self.assertRaises(Exception):
            media.finalize_recording([bad], output)
        self.assertEqual(output.read_bytes(), b"previous")
        self.assertEqual(bad.read_bytes(), b"broken")

    def test_opt_in_upscale_preserves_display_aspect_and_frame_count(self):
        source = self.make_video("source.ts", size="96x160", fps=12)
        output = self.path / "recording.mp4"
        with patch.object(media, "UPSCALE_SHORT_EDGE", 120):
            media.finalize_recording([source], output, upscale=True)
        stream = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-count_frames", "-of", "json", str(output),
        ]))["streams"][0]
        self.assertEqual((stream["width"], stream["height"], stream["nb_read_frames"]), (120, 200, "24"))


if __name__ == "__main__":
    unittest.main()
