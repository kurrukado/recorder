import asyncio
import base64
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import record_live_preview as live


SECRET = "fixture-preview-signing-key-at-least-32-bytes"


def token(username, *, mode="live", expiry=None):
    payload = json.dumps({"username": username, "sid": str(uuid.uuid4()), "mode": mode,
                          "exp": int(time.time()) + 3600 if expiry is None else expiry}).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.digest(SECRET.encode(), body, "sha256")).rstrip(b"=")
    return (body + b"." + signature).decode()


class LivePreviewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "FFmpeg binaries required")
    def test_real_relay_transcodes_to_a_small_browser_compatible_variant(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            source.mkdir()
            target.mkdir()
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=12",
                "-t", "4", "-threads:v", "1", "-c:v", "libx264", "-preset", "ultrafast", str(source / "source.ts"),
            ], check=True, timeout=12)
            with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=source)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}/source.ts"
                    with patch.object(live.recorder_core, "check_live_details", return_value={"is_live": True, "room_id": "1"}), \
                            patch.object(live.recorder_core, "get_stream_urls", return_value=[url]):
                        asyncio.run(asyncio.wait_for(live.run_relay("alpha", live.Relay(target)), 15))
                finally:
                    server.shutdown()
                    thread.join()
            self.assertIn("segment-000000.ts", (target / "index.m3u8").read_text(encoding="utf-8"))
            stream = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                "-of", "json", str(target / "segment-000000.ts"),
            ], timeout=10))["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertLessEqual(stream["width"], 854)
            self.assertLessEqual(stream["height"], 480)

    def test_shared_manifest_release_and_capacity(self):
        started = []
        stopped = []

        async def relay(username, state):
            started.append(username)
            (state.path / "segment-000000.ts").write_bytes(b"fixture segment")
            (state.path / "index.m3u8").write_text("#EXTM3U\nsegment-000000.ts\n", encoding="utf-8")
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(username)

        with patch.dict(os.environ, {"RECORD_MEDIA_SIGNING_SECRET": SECRET}), \
                patch.object(live, "run_relay", relay), patch.object(live, "IDLE_SECONDS", 0.3), TestClient(live.app) as client:
            first = f"/live/{token('alpha')}"
            second = f"/live/{token('alpha')}"
            other = f"/live/{token('beta')}"
            index = "/index.m3u8"
            self.assertIn("segment-000000.ts", client.get(first + index).text)
            self.assertEqual(client.get(second + index).status_code, 200)
            self.assertEqual(started, ["alpha"])
            file = client.get(first + "/segment-000000.ts")
            self.assertEqual((file.status_code, file.content, file.headers["cache-control"]), (200, b"fixture segment", "no-store"))
            self.assertEqual(client.get(other + index).status_code, 200)
            self.assertEqual(client.get(f"/live/{token('gamma')}{index}").status_code, 429)
            self.assertEqual(client.get(f"/live/{token('alpha', mode='play')}{index}").status_code, 401)
            self.assertEqual(client.get(f"/live/{token('alpha', expiry=1)}{index}").status_code, 401)
            self.assertEqual(client.get(first + "/not-a-segment").status_code, 404)
            self.assertEqual(client.delete(first + index).status_code, 204)
            self.assertEqual(client.delete(second + index).status_code, 204)
            deadline = time.monotonic() + 2
            while "alpha" not in stopped and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("alpha", stopped)
