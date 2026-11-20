"""Persistent finalize, archive and playback-cache lanes for the Recorder worker."""

import argparse
import hashlib
import itertools
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import requests

import gdrive_manager
import supabase_sync
from auto_h264 import FFMPEG_PATH, extract_middle_thumbnail
from config_lock import streamer_recording_lock

LOG = logging.getLogger(__name__)
R2_RETENTION = timedelta(days=14)
RESTORE_LEASE = 300
MAX_ATTEMPTS = 5
NORMALIZE_INPUTS = 8
UPSCALE_SHORT_EDGE = 1080
LOCAL_CACHE_LIMIT = 2
LOCAL_CACHE_TTL_SECONDS = 30 * 60
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
HANDLE = re.compile(r"[a-z0-9_.]{1,64}")
LOCAL_FILENAME = re.compile(r".+_part\d+_([0-9a-f]{24})\.mp4")
_LOCAL_CACHE_LOCK = threading.Lock()
_LOCAL_CACHE_IN_USE = set()


def _now():
    return datetime.now(timezone.utc)


def _write_json(path, data, *, exclusive=False):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        else:
            os.replace(tmp, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def enqueue_recording(spool, *, username, session_id, part_number, segments, started_at=None,
                      capture_ended_at=None, reconnect_indexes=None):
    """Durably hand off closed segments without moving them out of the capture tree."""
    spool = Path(spool).resolve()
    username = username.strip().lstrip("@").lower()
    if (not HANDLE.fullmatch(username) or username in {".", ".."}
            or not IDENTIFIER.fullmatch(session_id) or type(part_number) is not int or part_number < 1):
        raise ValueError("Invalid recording identity")
    identity = hashlib.sha256(f"{username}:{session_id}:{part_number}".encode()).hexdigest()[:24]
    folder = spool / "publish" / identity
    manifest = folder / "job.json"
    receipt = spool / "publish-done" / f"{identity}.json"
    if receipt.is_file():
        return receipt
    if manifest.is_file():
        return manifest
    paths = [Path(segment).resolve() for segment in segments]
    relative = [segment.relative_to(spool).as_posix() for segment in paths]
    if not relative or len(set(relative)) != len(relative) or not all(path.is_file() for path in paths):
        if receipt.is_file():
            return receipt
        raise ValueError("Recording requires unique closed segments inside the spool")
    recorded = datetime.fromisoformat(started_at) if started_at else _now()
    if recorded.tzinfo is None:
        raise ValueError("Recording time must include its timezone")
    stats = [path.stat() for path in paths]
    ended = datetime.fromisoformat(capture_ended_at) if capture_ended_at else datetime.fromtimestamp(
        max(stat.st_mtime for stat in stats), tz=timezone.utc,
    )
    if ended.tzinfo is None:
        raise ValueError("Capture end time must include its timezone")
    local_name_time = recorded.astimezone(timezone(timedelta(hours=7))).strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{username}_{local_name_time}_part{part_number:03d}_{identity}.mp4"
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(manifest, {
        "username": username, "filename": filename, "recorded_at": recorded.isoformat(),
        "capture_ended_at": ended.isoformat(), "input_bytes": sum(stat.st_size for stat in stats),
        "segments": relative, "reconnect_indexes": reconnect_indexes, "state": "queued", "attempts": 0,
    }, exclusive=True)
    return manifest


def _command(args, *, timeout):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr.strip()[-800:]}")
    return result


def _probe(path):
    result = _command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_data_hash", "sha256",
        "-of", "json", path,
    ], timeout=45)
    probe = json.loads(result.stdout)
    streams = probe.get("streams", [])
    video = next((item for item in streams if item["codec_type"] == "video"), None)
    audio = next((item for item in streams if item["codec_type"] == "audio"), None)
    if not video:
        raise ValueError(f"Missing video stream in {path.name}")
    duration = float(probe["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid video duration in {path.name}")
    return video, audio, duration


def _stream_parameters(probe):
    video_keys = ("codec_name", "profile", "level", "pix_fmt", "width", "height", "time_base",
                  "sample_aspect_ratio", "field_order", "color_range", "color_space", "color_transfer",
                  "color_primaries", "extradata_hash", "side_data_list")
    audio_keys = ("codec_name", "profile", "sample_fmt", "sample_rate", "channels", "channel_layout",
                  "time_base", "extradata_hash")
    video, audio, _ = probe
    def parameters(stream, keys):
        return tuple(stream.get(key) for key in keys) if stream else None

    return parameters(video, video_keys), parameters(audio, audio_keys)


def _compatible(probes):
    video, audio, _ = probes[0]
    if (video["codec_name"] != "h264" or video.get("pix_fmt") != "yuv420p"
            or not video.get("extradata_hash") or (audio and audio["codec_name"] != "aac")):
        return False
    signature = _stream_parameters(probes[0])
    return all(_stream_parameters(probe) == signature for probe in probes[1:])


def _known_seams(probes, reconnect_indexes):
    if not isinstance(reconnect_indexes, (list, tuple)) or any(
        type(index) is not int or not 0 < index < len(probes) for index in reconnect_indexes
    ):
        return None
    seams = set(reconnect_indexes)
    # Unreported changes suggest suspect capture metadata and warrant a full scan.
    changes = {index for index in range(1, len(probes))
               if _stream_parameters(probes[index - 1]) != _stream_parameters(probes[index])}
    return seams if changes <= seams else None


def _copy(segments, durations, output):
    list_file = output.with_suffix(".ffconcat")
    rows = []
    for segment, duration in zip(segments, durations, strict=True):
        path = segment.resolve().as_posix().replace("'", "'\\''")
        if "\n" in path or "\r" in path:
            raise ValueError("Invalid segment path")
        rows.extend((f"file '{path}'", f"duration {duration:.6f}"))
    list_file.write_text("\n".join(rows) + "\n", encoding="utf-8")
    try:
        result = _command([
            FFMPEG_PATH, "-nostdin", "-hide_banner", "-v", "warning", "-xerror", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file, "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-movflags", "+faststart", output,
        ], timeout=max(600, min(3600, sum(durations) / 2)))
        if re.search(r"non[ -]monoton|corrupt|invalid DTS", result.stderr, flags=re.IGNORECASE):
            raise ValueError("Stream-copy output contains timestamp or packet errors")
    finally:
        list_file.unlink(missing_ok=True)


def _normalize(segments, probes, output, *, upscale=False):
    video = probes[0][0]
    sar = video.get("sample_aspect_ratio")
    ratio = Fraction(sar.replace(":", "/")) if sar and sar not in {"0:1", "N/A"} else Fraction(1)
    width, height = max(2, int(video["width"] * ratio) // 2 * 2), max(2, video["height"] // 2 * 2)
    if any(abs(item.get("rotation", 0)) % 180 == 90 for item in video.get("side_data_list", [])):
        width, height = height, width
    if upscale:
        factor = max(1, min(UPSCALE_SHORT_EDGE / min(width, height), 2160 / max(width, height)))
        width, height = int(width * factor) // 2 * 2, int(height * factor) // 2 * 2

    if len(segments) <= NORMALIZE_INPUTS:
        _encode_segments(segments, probes, output, width, height, upscale=upscale)
        return
    # Each source is encoded once. Merge the bounded encoder groups by remuxing.
    with tempfile.TemporaryDirectory(prefix="normalize-", dir=output.parent) as directory:
        chunks, durations = [], []
        for index in range(0, len(segments), NORMALIZE_INPUTS):
            target = Path(directory) / f"group-{index:04d}.ts"
            end = index + NORMALIZE_INPUTS
            _encode_segments(segments[index:end], probes[index:end], target, width, height, upscale=upscale)
            chunks.append(target)
            video, audio, duration = _probe(target)
            timing = _validate_packets(target, {video["index"], audio["index"]})
            first, last, frame = timing[audio["index"]]
            # MPEG-TS metadata can exclude final AAC frames. Use actual packet boundaries for the join.
            durations.append(max(duration, last - first + frame) + 0.000_001)
        _copy(chunks, durations, output)


def _encode_segments(segments, probes, output, width, height, *, upscale=False):

    args = [FFMPEG_PATH, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-y", "-filter_complex_threads", "1"]
    for segment in segments:
        args.extend(("-threads", "1", "-i", segment))
    filters = []
    labels = []
    scaler = ":flags=lanczos" if upscale else ""
    for i, (video, audio, duration) in enumerate(probes):
        fit_width, fit_height = f"min({width},{height}*dar)", f"min({height},{width}/dar)"
        if not upscale:
            fit_width, fit_height = f"min(iw*sar,{fit_width})", f"min(ih,{fit_height})"
        scaled_width = f"max(2,trunc({fit_width}/2)*2)"
        scaled_height = f"max(2,trunc({fit_height}/2)*2)"
        filters.append(
            f"[{i}:v:0]setpts=PTS-STARTPTS,"
            f"scale=w='{scaled_width}':h='{scaled_height}'{scaler},setsar=1,format=yuv420p,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[v{i}]"
        )
        if audio:
            offset = float(audio.get("start_time", "0")) - float(video.get("start_time", "0"))
            audio_source = f"[{i}:a:0]asetpts=PTS-STARTPTS"
            if offset < 0:
                audio_source += f",atrim=start={-offset:.6f},asetpts=PTS-STARTPTS"
            elif offset > 0:
                audio_source += f",adelay=delays={round(offset * 1000)}:all=1"
            audio_source += ",aresample=48000:async=1:first_pts=0,apad"
        else:
            audio_source = "anullsrc=r=48000:cl=stereo"
        filters.append(f"{audio_source},aformat=sample_rates=48000:channel_layouts=stereo,atrim=duration={duration:.6f}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    filters.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[v][a]")
    container_args = ["-movflags", "+faststart", "-video_track_timescale", "90000"] if output.suffix == ".mp4" else []
    _command([
        *args, "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-threads:v", "2", "-preset", "veryfast", "-crf", "21", "-bf", "0",
        "-fps_mode", "vfr", "-enc_time_base:v", "1:90000", "-c:a", "aac", "-b:a", "128k",
        *container_args, output,
    ], timeout=4 * 3600)


def _validate_packets(path, indexes, *, interval=None):
    # ffprobe streams packet metadata to keep memory independent of recording length.
    interval_args = ["-read_intervals", f"{interval[0]:.6f}%{interval[1]:.6f}"] if interval else []
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as diagnostics:
        with subprocess.Popen([
            "ffprobe", "-v", "error", *interval_args, "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time,flags",
            "-of", "compact=p=0", str(path),
        ], stdout=subprocess.PIPE, stderr=diagnostics, text=True, encoding="utf-8") as proc:
            timing = {}
            try:
                for line in proc.stdout:
                    fields = dict(value.split("=", 1) for value in line.strip().split("|") if "=" in value)
                    if "stream_index" not in fields:
                        continue
                    index = int(fields["stream_index"])
                    pts, dts = float(fields["pts_time"]), float(fields["dts_time"])
                    first, last, _ = timing.get(index, (dts, dts - 1, 0))
                    gap = dts - last
                    if not math.isfinite(pts) or not math.isfinite(dts) or gap <= 0 or "C" in fields["flags"]:
                        raise ValueError("Corrupt packet or invalid packet timestamps")
                    duration_time = fields.get("duration_time", "N/A")
                    timing[index] = first, dts, 0 if duration_time == "N/A" else float(duration_time)
                if proc.wait(timeout=10) or indexes - timing.keys():
                    raise ValueError("Output is missing valid media packets")
                diagnostics.seek(0)
                if diagnostics.read(1):
                    raise ValueError("ffprobe reported media packet errors")
                return timing
            finally:
                if proc.poll() is None:
                    proc.kill()


def _validate_output(output, durations, *, seam_indexes=None):
    video, audio, duration = _probe(output)
    if (video["codec_name"] != "h264" or video.get("pix_fmt") != "yuv420p" or (audio and audio["codec_name"] != "aac")
            or abs(duration - sum(durations)) > max(0.5, 0.08 * len(durations))):
        raise ValueError("Unexpected codec or duration after finalizing")
    streams = {stream["index"] for stream in (video, audio) if stream}
    if seam_indexes is None:
        _validate_packets(output, streams)
        seam_indexes = range(1, len(durations))
        sample_packets = False
    else:
        sample_packets = True
    offsets = tuple(itertools.accumulate(durations))
    starts = {0, max(0, duration - 2)}
    starts.update(min(max(0, offsets[index - 1] - 1), max(0, duration - 1)) for index in seam_indexes)
    for start in sorted(starts):
        if sample_packets:
            _validate_packets(output, streams, interval=(start, min(duration, start + 3)))
        _command([
            FFMPEG_PATH, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-threads", "1",
            "-ss", f"{start:.6f}", "-i", output, "-t", "3",
            "-map", "0:v:0", "-map", "0:a:0?", "-abort_on", "empty_output", "-f", "null", "-",
        ], timeout=60)
    return duration


def _promote_mp4(tmp, output):
    with tmp.open("r+b") as file:
        os.fsync(file.fileno())
    os.replace(tmp, output)


def finalize_recording(segments, output, *, upscale=False, reconnect_indexes=None):
    """Validate a browser-ready MP4 before atomically replacing any prior output."""
    paths = [Path(segment) for segment in segments]
    output = Path(output)
    if not paths:
        raise ValueError("Recording has no segments")
    probes = [_probe(segment) for segment in paths]
    durations = [probe[2] for probe in probes]
    seam_indexes = _known_seams(probes, reconnect_indexes)
    tmp = output.with_suffix(".pending.mp4")
    try:
        first = probes[0][0]
        if upscale and min(first["width"], first["height"]) < UPSCALE_SHORT_EDGE:
            try:
                _normalize(paths, probes, tmp, upscale=True)
                duration = _validate_output(tmp, durations, seam_indexes=seam_indexes)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                LOG.info("Upscale rejected for %s: %s", output.name, error)
                seam_indexes = None
            else:
                _promote_mp4(tmp, output)
                return duration
        if _compatible(probes):
            try:
                _copy(paths, durations, tmp)
                duration = _validate_output(tmp, durations, seam_indexes=seam_indexes)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                LOG.info("Stream copy rejected for %s: %s", output.name, error)
                seam_indexes = None
            else:
                _promote_mp4(tmp, output)
                return duration
        _normalize(paths, probes, tmp)
        try:
            duration = _validate_output(tmp, durations, seam_indexes=seam_indexes)
        except ValueError:
            if seam_indexes is None:
                raise
            duration = _validate_output(tmp, durations)
        _promote_mp4(tmp, output)
        return duration
    finally:
        tmp.unlink(missing_ok=True)


def _drive_metadata(drive_id, token):
    with requests.get(
        f"https://www.googleapis.com/drive/v3/files/{drive_id}", headers={"Authorization": f"Bearer {token}"},
        params={"fields": "name,mimeType,trashed,size,md5Checksum,capabilities(canDownload)"}, timeout=(10, 25),
    ) as response:
        response.raise_for_status()
        return response.json()


def _local_identity(filename):
    match = LOCAL_FILENAME.fullmatch(filename)
    return match[1] if match else None


def _read_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


@contextmanager
def _borrow_local_mp4(spool, filename, username, drive_id, expected_size):
    identity = _local_identity(filename)
    candidate = None
    with _LOCAL_CACHE_LOCK:
        if identity:
            manifest = spool / "publish" / identity / "job.json"
            try:
                job = _read_json(manifest)
                path = manifest.parent / filename
                stat = path.stat()
                if (job["state"] in {"finalized", "published"} and job["filename"] == filename
                        and job["username"] == username and job.get("drive_file_id") == drive_id
                        and stat.st_size > 0 and job.get("archive_size") == stat.st_size
                        and job.get("archive_mtime_ns") == stat.st_mtime_ns
                        and (not expected_size or int(expected_size) == stat.st_size)
                        and path.is_file() and path.resolve().parent == manifest.parent):
                    candidate = path
                    _LOCAL_CACHE_IN_USE.add(manifest)
            except (OSError, KeyError, ValueError, TypeError):
                pass
    try:
        yield candidate
    finally:
        if candidate:
            with _LOCAL_CACHE_LOCK:
                _LOCAL_CACHE_IN_USE.discard(candidate.parent / "job.json")


class MediaBackend:
    def __init__(self):
        self.rest = f"{supabase_sync.SUPABASE_URL}/rest/v1"
        self.auth_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.auth_key.strip():
            raise ValueError("Missing SUPABASE_SERVICE_ROLE_KEY")
        self.r2_enabled = False
        try:
            self._configure_r2()
        except (KeyError, ValueError, ImportError) as error:
            LOG.warning("R2 transfers are deferred: %s", error)
        else:
            self.r2_enabled = True

    def _configure_r2(self):
        env_keys = ("RECORD_R2_BUCKET", "RECORD_R2_ENDPOINT", "RECORD_R2_ACCESS_KEY_ID", "RECORD_R2_SECRET_ACCESS_KEY")
        for key in env_keys:
            if not os.environ.get(key, "").strip():
                raise ValueError(f"Missing {key}")
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        self.bucket = os.environ["RECORD_R2_BUCKET"]
        endpoint = os.environ["RECORD_R2_ENDPOINT"]
        if not endpoint.startswith("https://"):
            raise ValueError("RECORD_R2_ENDPOINT must use HTTPS")
        self.s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name="auto",
            aws_access_key_id=os.environ["RECORD_R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["RECORD_R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=120,
                          request_checksum_calculation="when_required", response_checksum_validation="when_required",
                          retries={"mode": "standard", "total_max_attempts": 4}),
        )
        self.transfer = TransferConfig(multipart_threshold=16 * 1024**2, multipart_chunksize=16 * 1024**2,
                                       use_threads=False, preferred_transfer_client="classic")

    def request(self, method, path, *, params=None, body=None, prefer=None):
        headers = {"apikey": self.auth_key}
        if not self.auth_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.auth_key}"
        if prefer:
            headers["Prefer"] = prefer
        with requests.request(method, f"{self.rest}/{path}", headers=headers, params=params, json=body,
                              timeout=(10, 25)) as response:
            response.raise_for_status()
            return response.json() if response.content else None

    def is_monitored(self, username):
        return bool(self.request("GET", "tiktok_streamers", params={
            "username": f"eq.{username}", "or": "(is_account_deleted.is.null,is_account_deleted.eq.false)",
            "select": "username", "limit": 1,
        }))

    def archive(self, username, output):
        token = gdrive_manager.get_access_token()
        if not token:
            raise RuntimeError("Drive credentials unavailable")
        found, folder = gdrive_manager.create_streamer_folder_drive(username, access_token=token)
        if not found or not folder:
            raise RuntimeError("Drive archive folder unavailable")
        drive_id = gdrive_manager.upload_file_to_drive(str(output), folder, access_token=token, public=False,
                                                       min_video_bytes=1)
        if not isinstance(drive_id, str) or not IDENTIFIER.fullmatch(drive_id):
            raise RuntimeError("Drive archive did not return a file ID")
        self.verify_archive(drive_id, output, fallback_token=token)
        return drive_id

    def verify_archive(self, drive_id, output, *, fallback_token=None):
        if not IDENTIFIER.fullmatch(drive_id):
            raise ValueError("Invalid Drive archive ID")
        token = gdrive_manager.get_access_token() or fallback_token
        if not token:
            raise RuntimeError("Drive credentials unavailable")
        metadata = _drive_metadata(drive_id, token)
        if (metadata.get("trashed") or metadata.get("name") != output.name
                or int(metadata.get("size", 0)) != output.stat().st_size):
            raise RuntimeError("Drive archive did not confirm the uploaded filename and size")
        checksum = metadata.get("md5Checksum")
        if checksum and gdrive_manager.drive_file_checksum(output) != checksum:
            raise RuntimeError("Drive archive checksum did not match the uploaded MP4")

    def publish(self, job, output):
        thumb_path = output.with_suffix(".jpg")
        thumb_url = supabase_sync.upload_thumbnail_to_supabase(
            str(thumb_path), job["username"], job["filename"], auth_key=self.auth_key,
        ) if thumb_path.is_file() else None
        payload = {
            "p_filename": job["filename"], "p_username": job["username"], "p_drive_file_id": job["drive_file_id"],
            "p_size_bytes": job["archive_size"], "p_recorded_at": job["recorded_at"],
            "p_duration_seconds": job.get("duration_seconds") or _probe(output)[2],
        }
        if thumb_url:
            payload["p_thumbnail_url"] = thumb_url
        return self.request("POST", "rpc/record_worker_publish", body=payload) is True

    @staticmethod
    def object_key(filename, drive_id):
        return f"recordings/{hashlib.sha256(f'{filename}:{drive_id}'.encode()).hexdigest()}.mp4"

    def cached_until(self, key, expected_size):
        from botocore.exceptions import ClientError

        try:
            metadata = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            return None
        if metadata["ContentLength"] == expected_size:
            return metadata["LastModified"] + R2_RETENTION
        return None

    def cache(self, filename, drive_id, source):
        key = self.object_key(filename, drive_id)
        size = source.stat().st_size
        expires = self.cached_until(key, size)
        if not expires or expires <= _now() + timedelta(hours=1):
            self.s3.upload_file(str(source), self.bucket, key, ExtraArgs={"ContentType": "video/mp4"},
                                Config=self.transfer)
            expires = self.cached_until(key, size)
            if not expires or expires <= _now():
                raise RuntimeError("R2 did not confirm the uploaded object")
        return key, expires

    def set_ready(self, filename, drive_id, key, expires_at):
        self.request("POST", "record_media", params={"on_conflict": "filename"}, body={
            "filename": filename, "drive_file_id": drive_id, "r2_key": key, "state": "ready",
            "expires_at": expires_at.isoformat(), "updated_at": _now().isoformat(),
        }, prefer="resolution=merge-duplicates,return=minimal")

    def update_restore(self, job, owner, changes):
        now = _now().isoformat()
        return bool(self.request("PATCH", "record_worker_jobs", params={
            "job_key": f"eq.{job['job_key']}", "status": "eq.running", "lease_owner": f"eq.{owner}",
            "lease_until": f"gt.{now}",
        }, body={**changes, "updated_at": now}, prefer="return=representation"))

    def renew_restore(self, job, owner):
        expires = (_now() + timedelta(seconds=RESTORE_LEASE)).isoformat()
        return self.update_restore(job, owner, {"lease_until": expires})

    def restore(self, spool, job, owner):
        filename, username, drive_id = (job[key] for key in ("filename", "username", "drive_file_id"))
        if (not filename.lower().endswith(".mp4") or not re.fullmatch(r"[^/\\\x00-\x1f\x7f]{1,300}", filename)
                or not IDENTIFIER.fullmatch(drive_id)):
            raise InvalidRestore("Invalid restore identity")
        catalog = self.request("GET", "tiktok_recordings", params={
            "filename": f"eq.{filename}", "select": "username,drive_file_id,size_bytes", "limit": 1,
        })
        if not catalog or catalog[0]["username"] != username or catalog[0]["drive_file_id"] != drive_id:
            raise InvalidRestore("Requested restore does not match the recording catalog")
        with _borrow_local_mp4(spool, filename, username, drive_id, catalog[0].get("size_bytes")) as local:
            if local:
                if not self.renew_restore(job, owner):
                    raise LeaseLost()
                key, expires = self.cache(filename, drive_id, local)
                if not self.renew_restore(job, owner):
                    raise LeaseLost()
                self.set_ready(filename, drive_id, key, expires)
                return
        token = gdrive_manager.get_access_token()
        if not token or not gdrive_manager.is_recorder_owned_file(drive_id, access_token=token):
            raise RuntimeError("Drive archive ownership could not be verified")
        metadata = _drive_metadata(drive_id, token)
        size = int(metadata.get("size", 0))
        if (metadata.get("trashed") or not metadata.get("capabilities", {}).get("canDownload") or size <= 0
                or metadata.get("name") != filename or metadata.get("mimeType") not in {"video/mp4", "application/octet-stream"}):
            raise InvalidRestore("Drive denied media access or the archive does not match the recording")
        key = self.object_key(filename, drive_id)
        expires = self.cached_until(key, size)
        if not expires or expires <= _now() + timedelta(hours=1):
            from record_disk import spool_reserve_bytes

            if shutil.disk_usage(spool).free < size + spool_reserve_bytes(spool):
                raise DiskDeferred("Restore does not fit safely on the local spool")
            with tempfile.TemporaryDirectory(prefix="download-", dir=spool / "restore") as directory:
                target = Path(directory) / "archive.mp4"
                if not gdrive_manager.download_file_from_drive(drive_id, str(target), access_token=token):
                    raise RuntimeError("Drive media download failed")
                if target.stat().st_size != size:
                    raise ValueError("Incomplete Drive archive download")
                checksum = metadata.get("md5Checksum")
                if checksum and gdrive_manager.drive_file_checksum(target) != checksum:
                    raise ValueError("Drive archive checksum did not match")
                if not self.renew_restore(job, owner):
                    raise LeaseLost()
                key, expires = self.cache(filename, drive_id, target)
        if not self.renew_restore(job, owner):
            raise LeaseLost()
        self.set_ready(filename, drive_id, key, expires)


class LeaseLost(RuntimeError):
    pass


class DiskDeferred(RuntimeError):
    pass


class InvalidRestore(ValueError):
    pass


def _upscale_allowed(spool, segments):
    if os.environ.get("RECORD_MEDIA_UPSCALE") != "1":
        return False
    import psutil

    input_bytes = sum(segment.stat().st_size for segment in segments)
    return (os.cpu_count() or 1) >= 6 and psutil.cpu_percent(interval=0.2) < 25 \
        and psutil.virtual_memory().available >= 2 * 1024**3 \
        and shutil.disk_usage(spool).free >= max(5 * 1024**3, 4 * input_bytes)


def _finalize_job(spool, manifest, job, *, allow_upscale=False):
    output = manifest.parent / job["filename"]
    if not output.is_file():
        _cleanup_workdirs(manifest.parent, "normalize-*")
        segments = [spool / segment for segment in job["segments"]]
        from record_disk import finalize_scratch_bytes

        input_bytes = job.get("input_bytes") or sum(segment.stat().st_size for segment in segments)
        if shutil.disk_usage(spool).free < finalize_scratch_bytes(input_bytes):
            raise DiskDeferred("Finalization does not fit safely on the local spool")
        duration = finalize_recording(
            segments, output, upscale=allow_upscale and _upscale_allowed(spool, segments),
            reconnect_indexes=job.get("reconnect_indexes"),
        )
    else:
        duration = _probe(output)[2]
    extract_middle_thumbnail(str(output), str(output.with_suffix(".jpg")), max_edge=480)
    job.update(state="finalized", attempts=0, available=0, duration_seconds=duration)
    _write_json(manifest, job)


def _release_sources(spool, manifest, job):
    receipt = spool / "publish-done" / f"{manifest.parent.name}.json"
    with _LOCAL_CACHE_LOCK:
        if not receipt.exists():
            receipt.parent.mkdir(parents=True, exist_ok=True)
            data = {"filename": job["filename"], "drive_file_id": job["drive_file_id"],
                    "published_at": job.get("published_at")}
            early_marker = manifest.with_name("cache-ready.json")
            if early_marker.is_file():
                early = _read_json(early_marker)
                if early["filename"] == data["filename"] and early["drive_file_id"] == data["drive_file_id"]:
                    data["cache_ready_at"] = early["cache_ready_at"]
            _write_json(receipt, data)
    if not job.get("sources_released"):
        for segment in job["segments"]:
            path = spool / segment
            if not path.resolve().is_relative_to(spool):
                raise ValueError("Capture cleanup path is outside the spool")
            path.unlink(missing_ok=True)
        (manifest.parent / job["filename"]).with_suffix(".jpg").unlink(missing_ok=True)
        job["sources_released"] = True
        _write_json(manifest, job)


def _evict_local_mp4(spool, manifest):
    if manifest.parent.resolve().parent != spool / "publish" or manifest.parent.is_symlink():
        raise ValueError("Cleanup path is outside the publishing spool")
    with _LOCAL_CACHE_LOCK:
        if manifest not in _LOCAL_CACHE_IN_USE:
            shutil.rmtree(manifest.parent)


def _age_seconds(timestamp, end):
    if timestamp:
        try:
            return max(0, (end - datetime.fromisoformat(timestamp)).total_seconds())
        except (ValueError, TypeError):
            pass
    return None


def _record_cache_ready(spool, restore):
    filename, drive_id = restore["filename"], restore["drive_file_id"]
    identity = _local_identity(filename)
    if not identity:
        return
    receipt = spool / "publish-done" / f"{identity}.json"
    ready = _now()
    with _LOCAL_CACHE_LOCK:
        if receipt.is_file():
            target, job = receipt, _read_json(receipt)
        else:
            manifest = spool / "publish" / identity / "job.json"
            target = manifest.with_name("cache-ready.json")
            if not manifest.is_file() or target.exists():
                return
            local = _read_json(manifest)
            if local["state"] not in {"finalized", "published"} or not local.get("archive_size"):
                return
            job = {field: local.get(field) for field in ("filename", "drive_file_id", "published_at")}
        if job["filename"] != filename or job["drive_file_id"] != drive_id or job.get("cache_ready_at"):
            return
        job["cache_ready_at"] = ready.isoformat()
        _write_json(target, job)
    seconds = _age_seconds(restore.get("enqueued_at") or job.get("published_at"), ready)
    if seconds is not None:
        LOG.info("cache_ready filename=%s archive_to_ready_seconds=%.3f", filename, seconds)


def _publish_job(spool, manifest, job, backend):
    output = manifest.parent / job["filename"]
    if not backend.is_monitored(job["username"]):
        raise ValueError("Streamer is absent from the catalog")
    stat = output.stat()
    if not job.get("drive_file_id"):
        job["drive_file_id"] = backend.archive(job["username"], output)
    elif "archive_size" not in job:
        backend.verify_archive(job["drive_file_id"], output)
    if "archive_size" not in job:
        job.update(archive_size=stat.st_size, archive_mtime_ns=stat.st_mtime_ns)
        _write_json(manifest, job)
    stat = output.stat()
    if job["archive_size"] != stat.st_size or job["archive_mtime_ns"] != stat.st_mtime_ns:
        raise ValueError("Finalized MP4 no longer matches the confirmed Drive archive")
    if not backend.publish(job, output):
        raise ValueError("Streamer or recording ownership changed during publishing")
    published = _now()
    job.update(state="published", published_at=published.isoformat(), attempts=0, available=0)
    _write_json(manifest, job)
    seconds = _age_seconds(job.get("capture_ended_at"), published)
    if seconds is None:
        LOG.info("archive_published filename=%s", job["filename"])
    else:
        LOG.info("archive_published filename=%s end_to_archive_seconds=%.3f", job["filename"], seconds)
    _release_sources(spool, manifest, job)


def _retry_delay(attempts):
    return min(15 * 60, 15 * 2 ** min(attempts, 6))


def _run_local_job(action, manifest, job):
    try:
        action()
    except DiskDeferred as error:
        LOG.info("Media job %s deferred: %s", job["filename"], error)
        job["available"] = time.time() + 15
        _write_json(manifest, job)
    except Exception as error:
        LOG.warning("Media job %s (%s): %s", job["filename"], job["state"], error)
        attempts = job["attempts"] + 1
        job.update(attempts=attempts, available=time.time() + _retry_delay(attempts), last_error=str(error)[:500])
        if attempts >= MAX_ATTEMPTS and (job["state"] == "queued" or isinstance(error, ValueError)):
            job.update(state="failed")
        if manifest.is_file():
            _write_json(manifest, job)


def _restore_next(spool, backend):
    owner = f"media:{uuid.uuid4().hex}"
    claimed = backend.request("POST", "rpc/record_worker_claim_job", body={
        "p_kind": "restore", "p_owner": owner, "p_ttl_seconds": RESTORE_LEASE,
    })
    if not claimed:
        return False
    job = claimed[0]
    done = threading.Event()

    def keep_lease():
        while not done.wait(RESTORE_LEASE / 4):
            try:
                if not backend.renew_restore(job, owner):
                    break
            except requests.RequestException as error:
                LOG.warning("Restore lease refresh failed: %s", error)

    heartbeat = threading.Thread(target=keep_lease, name="restore-lease", daemon=True)
    heartbeat.start()
    try:
        backend.restore(spool, job, owner)
        changes = {"status": "ready", "last_error": None}
    except LeaseLost:
        LOG.warning("Restore lease lost for %s", job["filename"])
        return True
    except DiskDeferred as error:
        changes = {
            "status": "queued", "attempts": max(0, int(job["attempts"]) - 1),
            "available_at": (_now() + timedelta(seconds=30)).isoformat(), "last_error": str(error),
        }
    except Exception as error:
        LOG.warning("Restore job %s failed: %s", job["filename"], error)
        attempts = int(job["attempts"])
        changes = {
            "status": "failed" if isinstance(error, InvalidRestore) and attempts >= MAX_ATTEMPTS else "queued",
            "available_at": (_now() + timedelta(seconds=_retry_delay(attempts))).isoformat(),
            "last_error": str(error)[:500],
        }
    finally:
        done.set()
        heartbeat.join()
    updated = backend.update_restore(job, owner, {**changes, "lease_owner": None, "lease_until": None})
    if updated and changes["status"] == "ready":
        _record_cache_ready(spool, job)
    return True


def _cleanup_workdirs(folder, glob):
    for abandoned in folder.glob(glob):
        if abandoned.is_dir() and not abandoned.is_symlink() and abandoned.resolve().parent == folder:
            shutil.rmtree(abandoned)


def _maintain_local_cache(spool, published, *, evict_all=False):
    now = _now()
    retained = 0
    for manifest, job in sorted(published, key=lambda item: item[1].get("published_at", ""), reverse=True):
        def maintain():
            nonlocal retained
            _release_sources(spool, manifest, job)
            receipt = _read_json(spool / "publish-done" / f"{manifest.parent.name}.json")
            age = _age_seconds(job.get("published_at"), now)
            if (evict_all or job["state"] == "ready" or receipt.get("cache_ready_at")
                    or age is None or age >= LOCAL_CACHE_TTL_SECONDS or retained >= LOCAL_CACHE_LIMIT):
                _evict_local_mp4(spool, manifest)
            else:
                retained += 1

        _run_local_job(maintain, manifest, job)


def _run_lanes(spool, stop_event, backend):
    from record_disk import DiskPressure, spool_reserve_bytes

    disk = DiskPressure(spool)
    restore_dir = spool / "restore"
    restore_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_workdirs(restore_dir, "download-*")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalize") as finalizers, \
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="transfer") as transfers:
        finalize_task = None
        active = {}
        next_restore = 0
        while not stop_event.is_set():
            if finalize_task and finalize_task[0].done():
                finalize_task[0].result()
                finalize_task = None
            for future, identity in list(active.items()):
                if not future.done():
                    continue
                del active[future]
                try:
                    if future.result() and identity is None:
                        next_restore = 0
                except Exception as error:
                    LOG.warning("Media transfer failed: %s", error)
            busy = set(active.values())
            if finalize_task:
                busy.add(finalize_task[1])
            archives = []
            finalizations = []
            published = []
            pending = sum(identity is not None for identity in busy)
            for path in (spool / "publish").glob("*/job.json"):
                if path in busy:
                    continue
                job = _read_json(path)
                state = job["state"]
                if state in {"queued", "finalized"}:
                    pending += 1
                if time.time() >= job.get("available", 0):
                    if state == "finalized":
                        archives.append((path, job))
                    elif state == "queued":
                        finalizations.append((path, job))
                    elif state in {"published", "ready"}:
                        published.append((path, job))
            pressure = disk.sample(reserve_bytes=spool_reserve_bytes(spool))
            _maintain_local_cache(spool, published, evict_all=pressure.blocked)

            for path, job in sorted(archives, key=lambda item: item[1]["recorded_at"]):
                if len(active) >= 2:
                    break
                action = lambda p=path, j=job: _publish_job(spool, p, j, backend)
                active[transfers.submit(_run_local_job, action, path, job)] = path
            if finalize_task is None and finalizations:
                path, job = min(finalizations, key=lambda item: item[1]["recorded_at"])
                upscale = pending == 1 and not pressure.blocked
                action = lambda p=path, j=job, allow=upscale: _finalize_job(spool, p, j, allow_upscale=allow)
                finalize_task = finalizers.submit(_run_local_job, action, path, job), path

            if (backend.r2_enabled and not pressure.blocked and len(active) < 2 and None not in active.values()
                    and time.monotonic() >= next_restore):
                # Keep one transfer lane available for a Drive archive that finishes finalizing.
                active[transfers.submit(_restore_next, spool, backend)] = None
                next_restore = time.monotonic() + 5
            stop_event.wait(1)


def run_media_worker(spool, stop_event):
    """Run the bounded media lanes until the worker is asked to stop."""
    try:
        if not shutil.which("ffprobe") or not shutil.which(FFMPEG_PATH):
            raise ValueError("FFmpeg and ffprobe are required")
        backend = MediaBackend()
    except (KeyError, ValueError, ImportError) as error:
        LOG.error("Media worker configuration is incomplete: %s", error)
        return False
    spool = Path(spool).resolve()
    with streamer_recording_lock("record-worker-media", locks_dir=spool / ".locks") as locked:
        if not locked:
            LOG.error("Another media worker is using the recorder spool")
            return False
        try:
            _run_lanes(spool, stop_event, backend)
        except Exception:
            LOG.exception("Media worker stopped unexpectedly")
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    return 0 if run_media_worker(args.spool, stop_event) else 1


if __name__ == "__main__":
    raise SystemExit(main())
