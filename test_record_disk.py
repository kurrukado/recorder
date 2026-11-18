import json
import runpy
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from record_disk import DiskPressure, spool_reserve_bytes


MiB = 1024**2
Usage = namedtuple("Usage", "total used free")


class DiskPressureTests(unittest.TestCase):
    def test_hysteresis_low_space_and_critical_threshold(self):
        pressure = DiskPressure("/spool")
        total = 20_000 * MiB
        with patch("record_disk.shutil.disk_usage") as usage:
            for percent, reserve, blocked, critical in [
                (79, 0, False, False), (80, 0, True, False), (77, 0, True, False),
                (75, 0, True, False), (74, 0, False, False), (50, 12_000 * MiB, True, False),
                (45, 100, False, False), (89, 0, True, False), (90, 0, True, True),
            ]:
                with self.subTest(percent=percent, reserve=reserve):
                    used = total * percent // 100
                    usage.return_value = Usage(total, used, total - used)
                    sample = pressure.sample(reserve_bytes=reserve)
                    self.assertEqual(sample.used_percent, percent)
                    self.assertEqual(sample.free_bytes, total - used)
                    self.assertEqual((sample.blocked, sample.critical), (blocked, critical))

    def test_reserve_uses_largest_unfinished_part_and_ignores_bad_paths(self):
        with tempfile.TemporaryDirectory() as root:
            spool = Path(root)
            capture = spool / "capture"
            capture.mkdir()
            (capture / "a.ts").write_bytes(b"a" * 100)
            (capture / "b.ts").write_bytes(b"b" * 200)
            for name, data in {
                "a": {"state": "queued", "segments": ["capture/a.ts", "capture/b.ts"]},
                "b": {"state": "finalized", "input_bytes": 900},
                "c": {"state": "queued", "segments": ["../outside.ts"]},
                "d": {"state": "queued", "input_bytes": 400},
            }.items():
                folder = spool / "publish" / name
                folder.mkdir(parents=True)
                (folder / "job.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(spool_reserve_bytes(spool), 512 * MiB + 800)

    def test_measurement_counts_only_known_nonnegative_phase_latencies(self):
        measure = runpy.run_path(str(Path(__file__).parent / "deploy" / "kuru-record" / "measure.py"))
        result = measure["benchmark_latencies"]([
            "INFO capture_first_frame username=a add_to_first_seconds=2.12",
            "INFO capture_first_frame username=b add_to_first_seconds=7.3",
            "INFO capture_first_frame username=c add_to_first_seconds=unknown",
            "INFO archive_published filename=fixture.mp4 end_to_archive_seconds=4.0",
            "INFO cache_ready filename=fixture.mp4 archive_to_ready_seconds=3.2",
            "INFO cache_ready filename=other.mp4 archive_to_ready_seconds=-1",
            "INFO cache_ready filename=old.mp4 archive_to_ready_seconds=nan",
            "INFO different_event add_to_first_seconds=99",
        ])
        self.assertEqual(result, {
            "add_to_first_seconds": {"count": 2, "p95": 7.3},
            "end_to_archive_seconds": {"count": 1, "p95": 4},
            "archive_to_ready_seconds": {"count": 1, "p95": 3.2},
        })
