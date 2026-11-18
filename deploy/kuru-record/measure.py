"""Measure the dedicated capture host while exercising the actual worker lanes."""

import argparse
import json
import math
import re
import statistics
import time
from pathlib import Path

import psutil


PHASE_FIELDS = {
    "capture_first_frame": "add_to_first_seconds",
    "archive_published": "end_to_archive_seconds",
    "cache_ready": "archive_to_ready_seconds",
}
PHASE_PATTERN = re.compile(r"\b(" + "|".join(PHASE_FIELDS) + r")\s+(.*)")


def percentile95(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


def benchmark_latencies(lines):
    values = {field: [] for field in PHASE_FIELDS.values()}
    for line in lines:
        match = PHASE_PATTERN.search(line)
        if not match:
            continue
        field = PHASE_FIELDS[match[1]]
        parts = dict(part.split("=", 1) for part in match[2].split() if "=" in part)
        try:
            duration = float(parts.get(field, "nan"))
        except ValueError:
            continue
        if math.isfinite(duration) and duration >= 0:
            values[field].append(duration)
    return {field: {"count": len(samples), "p95": percentile95(samples)} for field, samples in values.items()}


def queue_depth(spool):
    depth = {"finalize": 0, "upload": 0, "failed": 0}
    for manifest in (spool / "publish").glob("*/job.json"):
        try:
            state = json.loads(manifest.read_text(encoding="utf-8"))["state"]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue
        bucket = {"queued": "finalize", "finalized": "upload", "failed": "failed"}.get(state)
        if bucket:
            depth[bucket] += 1
    return depth


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--latency-log", type=Path, help="Plain journalctl output covering the same test window")
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or args.minutes <= 0 or not args.spool.is_dir():
        parser.error("Use a positive duration and an existing spool directory")
    if args.latency_log is not None and not args.latency_log.is_file():
        parser.error("Use an existing journal file for --latency-log")
    initial = queue_depth(args.spool)
    peak = initial.copy()
    cpu, memory, disk = [], [], []
    latest = initial
    until = time.monotonic() + args.minutes * 60
    while (left := until - time.monotonic()) > 0:
        cpu.append(psutil.cpu_percent(interval=min(5, left)))
        memory.append(psutil.virtual_memory().percent)
        disk.append(psutil.disk_usage(str(args.spool)).percent)
        latest = queue_depth(args.spool)
        peak = {key: max(count, latest[key]) for key, count in peak.items()}
    result = {
        "minutes": args.minutes,
        "cpu_p95_pct": percentile95(cpu),
        "cpu_median_pct": statistics.median(cpu) if cpu else None,
        "ram_peak_pct": max(memory, default=None),
        "spool_disk_peak_pct": max(disk, default=None),
        "queue_initial": initial,
        "queue_peak": peak,
        "queue_final": latest,
    }
    if args.latency_log is not None:
        with args.latency_log.open(encoding="utf-8", errors="replace") as lines:
            result["phase_latencies"] = benchmark_latencies(lines)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
