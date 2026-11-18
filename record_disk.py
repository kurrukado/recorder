"""Admission thresholds shared by the capture and media services."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


SCRATCH_OVERHEAD = 512 * 1024**2


@dataclass(frozen=True)
class DiskSample:
    used_percent: float
    free_bytes: int
    blocked: bool
    critical: bool


class DiskPressure:
    def __init__(self, spool):
        self.spool = spool
        self.blocked = False

    def sample(self, reserve_bytes=0):
        total, _, free = shutil.disk_usage(self.spool)
        used_percent = 100 * (total - free) / total
        if used_percent >= 80 or free < reserve_bytes:
            self.blocked = True
        elif used_percent < 75:
            self.blocked = False
        emergency_space = min(SCRATCH_OVERHEAD, total // 20)
        return DiskSample(used_percent, free, self.blocked, used_percent >= 90 or free < emergency_space)


def finalize_scratch_bytes(input_bytes):
    # The output and bounded normalization groups can coexist until the final remux.
    return SCRATCH_OVERHEAD + 2 * input_bytes


def spool_reserve_bytes(spool):
    """Reserve scratch for the largest waiting finalization; one can run at a time."""
    root = Path(spool).resolve()
    largest_input = 0
    for manifest in (root / "publish").glob("*/job.json"):
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
            if job.get("state") != "queued":
                continue
            size = job.get("input_bytes")
            if not isinstance(size, int) or size < 0:
                paths = [(root / path).resolve() for path in job["segments"]]
                if any(not path.is_relative_to(root) for path in paths):
                    continue
                size = sum(path.stat().st_size for path in paths)
            largest_input = max(largest_input, size)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return finalize_scratch_bytes(largest_input)
