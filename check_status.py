#!/usr/bin/env python3
"""
check_status.py - Kiểm tra nhanh "hệ thống có đang chạy thật không" từ Supabase.

Không cần bật daemon. Đọc trực tiếp bảng tiktok_streamers và in ra:
  - Tổng số streamer, số đang live, số đang ghi hình
  - Tuổi heartbeat (updated_at) của từng streamer đang ghi -> biết runner còn sống
  - Cảnh báo ghost recording (is_recording=true nhưng heartbeat đã cũ)

Cách dùng:
    python check_status.py
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import supabase_sync

STALE_HEARTBEAT_SECONDS = 120  # quá 2 phút không cập nhật = runner có thể đã chết


def _age_seconds(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(time.time() - dt.timestamp())
    except Exception:
        return None


def main():
    print(f"[*] Supabase: {supabase_sync.SUPABASE_URL}")
    if not supabase_sync.SUPABASE_KEY:
        print("[!] Thiếu SUPABASE_KEY (env hoặc config.json).")
        return 1

    rows = supabase_sync.fetch_streamers_details_from_supabase()
    if rows is None:
        print("[!] Không truy vấn được Supabase.")
        return 1

    live = [r for r in rows if r.get("is_live")]
    rec = [r for r in rows if r.get("is_recording")]
    print(f"[✓] Tổng streamer: {len(rows)} | live: {len(live)} | recording: {len(rec)}")

    ghosts = []
    print("\n-- Đang ghi hình --")
    if not rec:
        print("   (không có)")
    for r in rec:
        age = _age_seconds(r.get("updated_at"))
        alive = age is not None and age <= STALE_HEARTBEAT_SECONDS
        mark = "OK " if alive else "STALE"
        if not alive:
            ghosts.append(r.get("username"))
        print(f"   [{mark}] @{r.get('username')}  status={r.get('status')}  "
              f"heartbeat={age}s  room_id={r.get('room_id')}")

    print("\n-- Đang live nhưng chưa ghi --")
    idle = [r for r in live if not r.get("is_recording")]
    if not idle:
        print("   (không có)")
    for r in idle:
        print(f"   @{r.get('username')}  status={r.get('status')}  "
              f"heartbeat={_age_seconds(r.get('updated_at'))}s")

    if ghosts:
        print(f"\n[!] GHOST RECORDING (heartbeat cũ > {STALE_HEARTBEAT_SECONDS}s): {ghosts}")
        print("    -> Chạy: python cleanup_ghost_recordings.py")
    else:
        print("\n[✓] Không có ghost recording.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
