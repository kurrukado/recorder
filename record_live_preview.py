"""Bounded live HLS relay for the persistent Recorder worker host."""

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse

import recorder_core


IDLE_SECONDS = 30
RELAY_LIMIT = 2
VIEWER_LIMIT = 100
ASSET = re.compile(r"segment-\d{6,10}\.ts")
LOW_VARIANT = re.compile(r"360p|480p|_ld\b|_sd\b|[ls]d[12]\b|stream_suffix=[ls]d\b", re.I)
LOG = logging.getLogger("record.preview")


def signed_viewer(token):
    secret = os.environ.get("RECORD_MEDIA_SIGNING_SECRET", "").encode()
    if len(secret) < 32 or len(token) > 1024 or token.count(".") != 1:
        raise ValueError("invalid token")
    payload, signature = token.split(".")
    expected = base64.urlsafe_b64encode(hmac.digest(secret, payload.encode(), "sha256")).rstrip(b"=")
    if not hmac.compare_digest(expected, signature.encode()):
        raise ValueError("invalid signature")
    value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    now = int(time.time())
    if value["mode"] != "live" or type(value["exp"]) is not int or not now < value["exp"] <= now + 14400:
        raise ValueError("expired or invalid scope")
    username = value["username"]
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", username) or username in (".", ".."):
        raise ValueError("invalid username")
    return username, str(uuid.UUID(value["sid"]))


@dataclass
class Relay:
    path: Path
    viewers: dict = field(default_factory=dict)
    last_seen: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None


async def stop_process(process):
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def can_copy(url):
    probe = await asyncio.create_subprocess_exec(
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-rw_timeout", "4000000",
        "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", "-i", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(probe.communicate(), 5)
        streams = json.loads(output).get("streams", [])
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next((s for s in streams if s["codec_type"] == "audio"), None)
        return (video["codec_name"] == "h264" and min(video["width"], video["height"]) <= 480
                and max(video["width"], video["height"]) <= 854 and (not audio or audio["codec_name"] == "aac"))
    except (ValueError, KeyError, StopIteration, TimeoutError):
        return False
    finally:
        await stop_process(probe)


async def run_relay(username, relay):
    process = None
    try:
        live = await asyncio.to_thread(recorder_core.check_live_details, username, max_wait=6)
        if not live.get("is_live") or not live.get("room_id"):
            return
        candidates = await asyncio.to_thread(
            recorder_core.get_stream_urls, live["room_id"], username, max_wait=6,
        )
        ordered = sorted(dict.fromkeys(candidates or []), key=lambda url: not LOW_VARIANT.search(url))
        playlist = relay.path / "index.m3u8"
        for url in ordered[:4]:
            if not url.startswith(("https://", "http://")):
                continue
            copy = await can_copy(url)
            codecs = ["-c", "copy"] if copy else [
                "-vf", "scale=w='if(gte(iw,ih),min(iw,854),min(iw,480))':h='if(gte(iw,ih),min(ih,480),min(ih,854))':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast", "-threads:v", "1", "-pix_fmt", "yuv420p",
                "-force_key_frames", "expr:gte(t,n_forced*2)", "-c:a", "aac", "-b:a", "96k",
            ]
            process = await asyncio.create_subprocess_exec(
                recorder_core.FFMPEG_PATH, "-nostdin", "-v", "error", "-rw_timeout", "7000000",
                "-i", url, "-map", "0:v:0", "-map", "0:a:0?", *codecs, "-f", "hls",
                "-hls_time", "2", "-hls_list_size", "5", "-hls_delete_threshold", "2",
                "-hls_flags", "delete_segments+omit_endlist+temp_file", "-hls_segment_filename",
                str(relay.path / "segment-%06d.ts"), str(playlist),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while not playlist.is_file() and process.returncode is None and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if playlist.is_file():
                await process.wait()
                return
            await stop_process(process)
        LOG.info("No playable live preview source for %s", username)
    except OSError:
        LOG.warning("Could not start live preview for %s", username)
    finally:
        await stop_process(process)


class RelayPool:
    def __init__(self, root):
        self.root = root
        self.relays = {}
        self.closing = set()

    def request(self, username, sid):
        relay = self.relays.get(username)
        if relay is None:
            if username in self.closing or len(self.relays) + len(self.closing) >= RELAY_LIMIT:
                raise HTTPException(429, "Live preview is at capacity", headers={"Retry-After": "3"})
            path = Path(tempfile.mkdtemp(prefix="relay-", dir=self.root))
            relay = self.relays[username] = Relay(path)
            relay.task = asyncio.create_task(run_relay(username, relay))
        if relay.task.done():
            raise HTTPException(503, "Live preview is unavailable", headers={"Retry-After": "5"})
        if sid not in relay.viewers and len(relay.viewers) >= VIEWER_LIMIT:
            raise HTTPException(429, "Live preview is at capacity")
        relay.last_seen = relay.viewers[sid] = time.monotonic()
        return relay

    async def remove(self, username):
        relay = self.relays.pop(username)
        self.closing.add(username)
        try:
            relay.task.cancel()
            await asyncio.gather(relay.task, return_exceptions=True)
            await asyncio.to_thread(shutil.rmtree, relay.path, ignore_errors=True)
        finally:
            self.closing.discard(username)

    async def reap(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for username, relay in list(self.relays.items()):
                relay.viewers = {sid: seen for sid, seen in relay.viewers.items() if now - seen < IDLE_SECONDS}
                if not relay.viewers and now - relay.last_seen >= IDLE_SECONDS:
                    await self.remove(username)
                elif relay.task.done() and now - relay.last_seen >= 5:
                    await self.remove(username)


@asynccontextmanager
async def lifespan(app):
    with tempfile.TemporaryDirectory(prefix="kuru-preview-") as root:
        app.state.pool = RelayPool(root)
        task = asyncio.create_task(app.state.pool.reap())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for username in list(app.state.pool.relays):
                await app.state.pool.remove(username)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("RECORD_PREVIEW_ALLOWED_ORIGIN", "").split(",") if origin.strip()],
    allow_methods=["GET", "DELETE"],
)


def viewer(token, asset):
    if asset != "index.m3u8" and not ASSET.fullmatch(asset):
        raise HTTPException(404)
    try:
        return signed_viewer(token)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid live preview token") from None


@app.get("/live/{token}/{asset}")
async def live_asset(token: str, asset: str):
    username, sid = viewer(token, asset)
    pool = app.state.pool
    if asset != "index.m3u8" and username not in pool.relays:
        raise HTTPException(404)
    relay = pool.request(username, sid)
    path = relay.path / asset
    if asset == "index.m3u8":
        deadline = time.monotonic() + 20
        while not path.is_file() and not relay.task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if relay.task.done():
            raise HTTPException(503, "Live preview is unavailable", headers={"Retry-After": "3"})
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise HTTPException(503, "Live preview is warming up", headers={"Retry-After": "3"}) from None
        return Response(data, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "no-store"})


@app.delete("/live/{token}/index.m3u8", status_code=204)
async def release(token: str):
    username, sid = viewer(token, "index.m3u8")
    relay = app.state.pool.relays.get(username)
    if relay:
        relay.viewers.pop(sid, None)
    return Response(status_code=204)
