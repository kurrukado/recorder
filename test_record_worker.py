import asyncio
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import recorder_core
from record_worker import Database, ProbeSchedule, Stream, Worker, begin_capture, ordered_sources, part_segments, recover_parts
from record_worker_media import enqueue_recording


class ProbeTests(unittest.TestCase):
    def test_network_failure_never_confirms_offline(self):
        schedule = ProbeSchedule()
        self.assertEqual(schedule.observe("live", 0), 10)
        self.assertEqual(schedule.observe("offline", 10), 20)
        self.assertEqual(schedule.observe("unavailable", 20), 40)
        self.assertEqual(schedule.observe("offline", 40), 50)
        self.assertEqual(schedule.observe("offline", 50), 110)
        self.assertTrue(schedule.confirmed_offline)

    def test_core_distinguishes_http_failure_from_clear_room_status(self):
        class Response:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self.payload = payload

            def json(self):
                return self.payload

            def close(self):
                pass

        def fail(*args, **kwargs):
            raise OSError("no connection")

        with patch.object(recorder_core.requests, "get", return_value=Response(503)), \
             patch.object(recorder_core.requests, "Session", side_effect=fail), \
             patch("urllib.request.urlopen", side_effect=fail):
            self.assertIsNone(recorder_core.check_live_details("example", cookies={})["probe_state"])
        payload = {"data": {"liveRoom": {"status": 4}, "user": {"uniqueId": "example"}}}
        with patch.object(recorder_core.requests, "get", return_value=Response(200, payload)):
            self.assertEqual(recorder_core.check_live_details("example", cookies={})["probe_state"], "offline")

    def test_explicit_1080_hints_run_before_origins_and_ignore_non_network_urls(self):
        source = "https://live.example.net/stream-010001.m3u8"
        full = "https://live.example.net/stream-010001_full_hd1.flv"
        self.assertEqual(ordered_sources([source, full, full, "file:///tmp/private"]), [full, source])


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_and_seven_hours_of_media_use_one_and_two_parts(self):
        class DB:
            async def rpc(self, name, **args):
                return True

        class FixtureWorker(Worker):
            async def source_urls(self, stream, room, budget):
                return ["https://fixture.invalid/source.m3u8"]

            async def discover(self, stream, *, max_wait):
                return "offline", None

        async def immediate(_event, _seconds):
            return False

        for hours, count in [(3, 1), (7, 2)]:
            with self.subTest(hours=hours), tempfile.TemporaryDirectory() as temp:
                stream = Stream("bob", True, room="room1", live=True)
                worker = FixtureWorker(DB(), Path(temp), "active", set(), 1)
                remaining = hours * 3600
                lengths = {}
                published = []

                async def clip(sources, part, *, duration, **kwargs):
                    nonlocal remaining
                    self.assertGreater(remaining, 0)
                    seconds = min(remaining, duration)
                    remaining -= seconds
                    lengths[part.name] = seconds
                    part.mkdir(parents=True)

                    async def close():
                        pass

                    now = time.monotonic()
                    return types.SimpleNamespace(
                        close=close, presentation_seconds=lambda: seconds, url=sources[0],
                        first_frame_at=now, last_progress=now, resolution=(1920, 1080),
                        process=types.SimpleNamespace(returncode=0),
                    )

                async def publish(_stream, _session_id, part):
                    published.append(lengths[part.name])

                with patch("record_worker.begin_capture", side_effect=clip), \
                        patch("record_worker.sleep_or_stop", side_effect=immediate), \
                        patch.object(worker, "finalize", side_effect=publish):
                    self.assertEqual(await asyncio.wait_for(worker.capture_session(stream, "owner"), 3), "offline")
                self.assertEqual(len(published), count)
                self.assertEqual(sum(published), hours * 3600)
                self.assertTrue(all(0 < duration <= 4 * 3600 for duration in published))

    async def test_uses_real_high_definition_source_and_stream_copies_short_clip(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            for size in ("640x360", "1920x1080"):
                await asyncio.to_thread(subprocess.run, [
                    recorder_core.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=s={size}:r=12:d=2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-g", "12", str(work / f"{size}.mp4"),
                ], check=True)
            part = work / "capture" / "bob" / "20261118T000000Z_abcd" / "0001"
            process = await begin_capture(
                [str(work / "640x360.mp4"), str(work / "missing.mp4"), str(work / "1920x1080.mp4")],
                part, duration=1.5, stop=asyncio.Event(),
            )
            self.assertIsNotNone(process)
            try:
                await process.wait()
                self.assertEqual(process.resolution, (1920, 1080))
                self.assertAlmostEqual(process.media_seconds, 1.5, delta=0.15)
                self.assertEqual(len(part_segments(part)), 1)
            finally:
                await process.close()


    async def test_runtime_rolls_parts_using_media_duration_and_publishes_closed_inputs(self):
        class DB:
            async def rpc(self, name, **args):
                return True

        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp)
            source = spool / "source.mp4"
            await asyncio.to_thread(subprocess.run, [
                recorder_core.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=s=192x108:r=12:d=5", "-c:v", "libx264", "-g", "12", str(source),
            ], check=True)

            class FixtureWorker(Worker):
                async def source_urls(self, stream, room, budget):
                    return [str(source)]

            stream = Stream("bob", True, room="room1", live=True)
            worker = FixtureWorker(DB(), spool, "active", set(), 1)
            loop = asyncio.get_running_loop()

            def enqueue_and_stop(*args, **kwargs):
                manifest = enqueue_recording(*args, **kwargs)
                if kwargs["part_number"] == 2:
                    loop.call_soon_threadsafe(stream.stop.set)
                return manifest

            with patch("record_worker.PART_SECONDS", 4.0), patch("record_worker_media.enqueue_recording", side_effect=enqueue_and_stop):
                await asyncio.wait_for(worker.capture_session(stream, "owner"), 3)

            manifest_data = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (spool / "publish").glob("*/job.json")
            ]
            self.assertEqual(len(manifest_data), 2)
            self.assertTrue(any("_part001_" in job["filename"] for job in manifest_data))
            self.assertTrue(any("_part002_" in job["filename"] for job in manifest_data))
            for job in manifest_data:
                self.assertEqual(len(job["segments"]), 1)
                self.assertEqual(job["reconnect_indexes"], [])
                self.assertIn("capture_ended_at", job)
                segment = spool / job["segments"][0]
                result = await asyncio.to_thread(subprocess.check_output, [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(segment),
                ], text=True)
                self.assertGreater(float(result.strip()), 3.3)
                self.assertLessEqual(float(result.strip()), 4.0)


class RecoveryTests(unittest.TestCase):
    def test_restart_recovers_uncommitted_tail_once_and_excludes_rejected_source(self):
        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp)
            part = spool / "capture" / "bob" / "20261118T000000Z_abcd" / "0001"
            expected = []
            for folder, number, selected in (("00000_0", 2, True), ("00000_1", 1, False), ("00001_0", 3, False)):
                attempt = part / folder
                attempt.mkdir(parents=True)
                source = attempt / "00000.ts"
                source.write_bytes(b"q" * (188 * number))
                if selected or number == 3:
                    expected.append(source)
                if selected:
                    (attempt / "selected.json").write_text("{}", encoding="utf-8")
            enqueue = []
            fake_publisher = types.SimpleNamespace(enqueue_recording=lambda path, **job: enqueue.append(job))
            with patch.dict(sys.modules, {"record_worker_media": fake_publisher}):
                for _ in range(2):
                    recover_parts(spool, lambda user: True, {"bob"})
            self.assertEqual([job["segments"] for job in enqueue], [expected])
            self.assertEqual([job["reconnect_indexes"] for job in enqueue], [[1]])


    def test_restart_quarantines_corrupt_tail_but_recovers_closed_video(self):
        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp)
            attempt = spool / "capture" / "bob" / "20261118T000000Z_abcd" / "0001" / "00000_0"
            attempt.mkdir(parents=True)
            (attempt / "selected.json").write_text("{}", encoding="utf-8")
            (attempt / "pid.json").write_text('{"pid": -1}', encoding="utf-8")
            intact = attempt / "00000.ts"
            subprocess.run([
                recorder_core.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=s=64x64:r=12:d=2", "-c:v", "libx264", "-f", "mpegts", str(intact),
            ], check=True)
            trailing = attempt / "00001.ts"
            trailing.write_bytes(b"incomplete" * 50)
            enqueue = []
            fake_publisher = types.SimpleNamespace(enqueue_recording=lambda path, **job: enqueue.append(job))
            with patch.dict(sys.modules, {"record_worker_media": fake_publisher}):
                recover_parts(spool, lambda user: True, {"bob"})
            self.assertEqual([job["segments"] for job in enqueue], [[intact]])
            self.assertEqual(trailing.with_suffix(".ts.incomplete").read_bytes(), b"incomplete" * 50)


class WorkerSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_keeps_slot_until_cleanup_and_promoted_stream_rechecks_live(self):
        refreshes = {}

        class DB:
            async def rpc(self, name, **args):
                return True

        class FixtureWorker(Worker):
            async def discover(self, stream, **kwargs):
                refreshes[stream.username] = refreshes.get(stream.username, 0) + 1
                return "live", f"room-{stream.username}"

            async def capture_session(self, stream, owner):
                started[stream.username].set()
                await stream.stop.wait()
                if stream.username == "alice":
                    await drained.wait()
                return "paused"

        async def tick_until(worker, predicate):
            async def tick():
                while not predicate():
                    await worker.dispatch()
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(tick(), 2)

        started = {user: asyncio.Event() for user in ("alice", "bob")}
        drained = asyncio.Event()
        with tempfile.TemporaryDirectory() as temp:
            worker = FixtureWorker(DB(), Path(temp), "active", set(), 1)
            worker.disk_pressure = DiskSamples()
            worker.apply_preferences({"alice": True})
            await tick_until(worker, started["alice"].is_set)
            worker.apply_preferences({"alice": True, "bob": True})
            await tick_until(worker, lambda: worker.users["bob"].status == "waiting_slot")
            worker.apply_preferences({"alice": False, "bob": True})
            await worker.dispatch()
            self.assertFalse(started["bob"].is_set())
            drained.set()
            await tick_until(worker, started["bob"].is_set)
            self.assertEqual(refreshes["bob"], 2)
            self.assertEqual(worker.users["alice"].status, "paused")
            worker.users["bob"].stop.set()
            await worker.users["bob"].capture

    async def test_new_realtime_refresh_wakes_without_waiting_for_fallback(self):
        class DB:
            def __init__(self):
                self.data = {"old": False}
                self.calls = 0
                self.connected = asyncio.Event()

            async def preferences(self):
                self.calls += 1
                return self.data.copy()

            async def changes(self, preferences, notify, connectivity=None):
                self.notify = notify
                self.connected.set()
                await asyncio.Future()

        class FixtureWorker(Worker):
            async def discover(self, stream, **kwargs):
                observed.set()
                return "live", "new-room"

        observed = asyncio.Event()
        db = DB()
        with tempfile.TemporaryDirectory() as temp:
            worker = FixtureWorker(db, Path(temp), "shadow", set(), 1)
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(db.connected.wait(), 1)
                db.data["new"] = True
                db.notify()
                await asyncio.wait_for(observed.wait(), 1)
                await asyncio.sleep(0.05)
                self.assertEqual(db.calls, 2)
            finally:
                worker.stopping.set()
                worker.wake.set()
                await task


class DiskSamples:
    def __init__(self):
        self.value = types.SimpleNamespace(blocked=False, critical=False, used_percent=60, free_bytes=8000)

    def sample(self, reserve_bytes=0):
        return self.value


class DiskAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_storage_queues_live_then_releases_admission(self):
        statuses = []

        class DB:
            async def rpc(self, name, **args):
                statuses.append(args["p_status"])
                return True

        class FixtureWorker(Worker):
            async def capture(self, stream):
                started.set()
                await stream.stop.wait()

        started = asyncio.Event()
        with tempfile.TemporaryDirectory() as temp:
            worker = FixtureWorker(DB(), Path(temp), "active", set(), 2)
            stream = Stream("alice", True, live=True, room="room", waiting_since=1)
            stream.add_requested_at = datetime.now(timezone.utc)
            stream.probe.due = float("inf")
            worker.users["alice"] = stream
            worker.disk_pressure = DiskSamples()
            worker.disk_pressure.value = types.SimpleNamespace(blocked=True, critical=False, used_percent=82, free_bytes=5000)
            await worker.dispatch()
            self.assertEqual(stream.status, "waiting_storage")
            self.assertIsNone(stream.add_requested_at)
            self.assertFalse(started.is_set())
            self.assertIsNone(stream.capture)
            stream.status_sent_at = time.monotonic() - 61
            await worker.dispatch()
            self.assertEqual(statuses, ["waiting_storage", "waiting_storage"])

            worker.disk_pressure.value = DiskSamples().value
            worker.disk_sample_due = 0
            await worker.dispatch()
            await asyncio.wait_for(started.wait(), 1)
            stream.stop.set()
            await stream.capture

    async def test_critical_storage_stops_newest_one_at_a_time_only_after_worsening(self):
        def sample(free):
            return types.SimpleNamespace(blocked=True, critical=True, used_percent=95, free_bytes=free)

        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(None, Path(temp), "active", set(), 3)
            worker.disk_pressure = DiskSamples()
            worker.disk_pressure.value = sample(100)
            old = Stream("alice", True, capture_started_at=1)
            new = Stream("bob", True, capture_started_at=2)
            for stream in (old, new):
                stream.probe.due = float("inf")
                stream.capture = asyncio.create_task(stream.stop.wait())
                worker.users[stream.username] = stream

            try:
                await worker.dispatch()
                self.assertTrue(new.stop.is_set())
                self.assertFalse(old.stop.is_set())

                worker.disk_pressure.value = sample(99)
                worker.disk_sample_due = 0
                await worker.dispatch()
                self.assertFalse(old.stop.is_set())

                await new.capture
                new.capture = None
                worker.disk_sample_due = 0
                await worker.dispatch()
                self.assertFalse(old.stop.is_set())

                worker.next_storage_stop = 0
                worker.disk_pressure.value = sample(100)
                worker.disk_sample_due = 0
                await worker.dispatch()
                self.assertFalse(old.stop.is_set())

                worker.disk_pressure.value = sample(99)
                worker.disk_sample_due = 0
                await worker.dispatch()
                self.assertTrue(old.stop.is_set())
            finally:
                for stream in (old, new):
                    stream.stop.set()
                    if stream.capture is not None:
                        await stream.capture


class RealtimePollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_requires_join_ack_before_using_healthy_intervals(self):
        class Socket:
            def __init__(self):
                self.messages = asyncio.Queue()
                self.sent = asyncio.Queue()
                self.closed = False

            async def send_json(self, data):
                await self.sent.put(data)

            async def receive_json(self):
                message = await self.messages.get()
                if isinstance(message, Exception):
                    raise message
                return message

        socket = Socket()

        class Connection:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                pass

        class Session:
            def ws_connect(self, *args, **kwargs):
                if kwargs.get("params", {}).get("apikey") != "sb_secret_fixture":
                    raise AssertionError("WebSocket gateway must receive the secret API key")
                return Connection()

        transitions = []
        notifications = []
        db = Database(Session(), "https://db.invalid", "sb_secret_fixture")
        self.assertEqual(db.headers, {"apikey": "sb_secret_fixture"})
        self.assertEqual(Database(Session(), "https://db.invalid", "legacy.fixture.key").headers["Authorization"],
                         "Bearer legacy.fixture.key")
        task = asyncio.create_task(db.changes(lambda: {"alice": True}, notifications.append, transitions.append))
        try:
            join = await asyncio.wait_for(socket.sent.get(), 1)
            self.assertEqual(join["event"], "phx_join")
            self.assertNotIn("access_token", join["payload"])
            self.assertNotIn(True, transitions)
            socket.messages.put_nowait({
                "topic": join["topic"], "event": "phx_reply", "ref": join["ref"],
                "payload": {"status": "ok", "response": {"postgres_changes": [
                    {"table": "tiktok_streamers"}, {"table": "record_worker_jobs"},
                ]}},
            })
            await asyncio.sleep(0.01)
            self.assertNotIn(True, transitions)
            socket.messages.put_nowait({
                "topic": join["topic"], "event": "system",
                "payload": {"status": "ok", "extension": "postgres_changes"},
            })
            socket.messages.put_nowait({
                "topic": join["topic"], "event": "postgres_changes", "payload": {"data": {
                    "table": "tiktok_streamers", "type": "UPDATE", "record": {"username": "alice", "recording_enabled": False},
                }},
            })
            socket.messages.put_nowait({
                "topic": join["topic"], "event": "postgres_changes", "payload": {"data": {
                    "table": "tiktok_streamers", "type": "DELETE", "old_record": {"id": 1},
                }},
            })
            socket.messages.put_nowait(ConnectionError("closed"))
            await asyncio.sleep(0.05)
            self.assertEqual(transitions, [True, False])
            self.assertEqual(notifications, ["preferences", "preferences"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_realtime_health_slows_fallback_and_disconnect_reads_paused_preferences_promptly(self):
        class DB:
            def __init__(self):
                self.reads = 0
                self.claims = 0
                self.connected = asyncio.Event()
                self.data = {"alice": True}

            async def preferences(self):
                self.reads += 1
                return self.data.copy()

            async def changes(self, preferences, notify, connectivity):
                self.connectivity = connectivity
                self.connectivity(True)
                self.connected.set()
                await asyncio.Future()

            async def rpc(self, name, **kwargs):
                if name == "claim_job":
                    self.claims += 1
                    return []
                return True

        class FixtureWorker(Worker):
            async def discover(self, stream, **kwargs):
                return "offline", None

        async def wait_for(predicate):
            while not predicate():
                await asyncio.sleep(0.005)

        db = DB()
        with tempfile.TemporaryDirectory() as temp, \
             patch("record_worker.FALLBACK_POLL_SECONDS", 0.02), \
             patch("record_worker.SCHEDULER_SECONDS", 0.01):
            worker = FixtureWorker(db, Path(temp), "active", set(), 1)
            worker.disk_pressure = DiskSamples()
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(db.connected.wait(), 1)
                await asyncio.wait_for(wait_for(lambda: db.reads >= 1 and db.claims >= 1), 1)
                await asyncio.sleep(0.08)
                stable_reads, stable_claims = db.reads, db.claims
                await asyncio.sleep(0.08)
                self.assertEqual((db.reads, db.claims), (stable_reads, stable_claims))

                db.data["alice"] = False
                db.connectivity(False)
                await asyncio.wait_for(wait_for(lambda: worker.users["alice"].enabled is False), 1)
                await asyncio.wait_for(wait_for(lambda: db.reads >= stable_reads + 2 and db.claims >= stable_claims + 2), 1)
            finally:
                worker.stopping.set()
                worker.wake.set()
                await task
