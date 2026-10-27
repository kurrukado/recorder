#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_ghost_recordings.py
===========================
Production-grade maintenance utility to reconcile Supabase `tiktok_streamers`
and Google Drive `active_recordings.json` with ground truth from the TikTok API.

Key Functions:
1. Queries all streamers from Supabase `tiktok_streamers`.
2. Inspects local FFmpeg processes to detect genuine active recordings.
3. Checks live status against TikTok API (`recorder_core.check_live_details`).
4. Reconciles Supabase:
   - Resets `is_recording = false` for any streamer without an active recording process.
   - For confirmed LIVE streamers: `is_live = true, status = 'live', room_id = room_id`.
   - For confirmed OFFLINE streamers: `is_live = false, is_recording = false, status = 'offline', room_id = None`.
5. Cleans up stale test entries (`mid_record_del_streamer`, `test_target_user`, `linhphuongfiller`)
   and offline streamers from Google Drive `active_recordings.json` using `gdrive_manager`.
6. Provides comprehensive verification and health reporting before and after execution.
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Set, Any, Optional, Tuple

import requests

import supabase_sync
import recorder_core
import gdrive_manager

# Stale test streamers to unconditionally purge from Google Drive
KNOWN_STALE_TEST_STREAMERS = {
    "mid_record_del_streamer",
    "test_target_user",
    "linhphuongfiller",
}


def get_local_active_recording_users() -> Set[str]:
    """
    Inspects running OS processes for active FFmpeg recorder instances.
    Returns a set of lowercase usernames currently being recorded locally.
    """
    active_users: Set[str] = set()
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            p_name = (proc.info.get("name") or "").lower()
            if "ffmpeg" in p_name:
                cmd = " ".join(proc.info.get("cmdline") or []).lower()
                # Check for common patterns in ffmpeg commands
                # E.g., filenames like username_YYYY-MM-DD or output arguments
                # We also check against monitored streamers if available
                streamers = supabase_sync.fetch_streamers_from_supabase() or []
                for user in streamers:
                    u_clean = user.strip().replace("@", "").lower()
                    if u_clean and (f"{u_clean}_" in cmd or f"/{u_clean}/" in cmd or f"\\{u_clean}\\" in cmd):
                        active_users.add(u_clean)
    except Exception as e:
        print(f"[CLEANUP] [!] Note: Process scan completed with note: {e}")
    return active_users


get_active_local_recordings = get_local_active_recording_users


def fetch_all_streamers_raw() -> List[Dict[str, Any]]:
    """Fetches all rows from Supabase `tiktok_streamers`."""
    headers = supabase_sync.get_supabase_headers()
    url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_streamers?select=*&order=id.desc"
    res = requests.get(url, headers=headers, timeout=12)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response format from Supabase: {data}")
    return data


def compute_target_state(
    current_row: Dict[str, Any],
    live_details: Dict[str, Any],
    active_local_users: Set[str],
    now_iso: str
) -> Tuple[Dict[str, Any], bool]:
    """
    Computes the target state for a streamer based on ground truth.
    Returns: (target_payload, has_changes)
    """
    user = current_row["username"].strip().replace("@", "").lower()
    is_live_truth = bool(live_details.get("is_live"))
    room_id_truth = str(live_details.get("room_id")) if live_details.get("room_id") else None
    is_account_deleted = bool(live_details.get("is_account_deleted"))
    is_locally_recording = user in active_local_users

    # Determine desired state
    if is_locally_recording:
        target_is_rec = True
        target_status = "recording"
        target_is_live = True
        target_room_id = room_id_truth or current_row.get("room_id")
    elif is_account_deleted:
        target_is_rec = False
        target_is_live = False
        target_status = "deleted"
        target_room_id = None
    elif is_live_truth:
        target_is_rec = False
        target_is_live = True
        target_status = "live"
        target_room_id = room_id_truth
    else:
        target_is_rec = False
        target_is_live = False
        target_status = "offline"
        target_room_id = None

    # Check differences against current database state
    cur_is_rec = bool(current_row.get("is_recording"))
    cur_is_live = bool(current_row.get("is_live"))
    cur_status = current_row.get("status")
    cur_room_id = str(current_row.get("room_id")) if current_row.get("room_id") else None
    cur_del = bool(current_row.get("is_account_deleted"))

    changes_needed = (
        cur_is_rec != target_is_rec
        or cur_is_live != target_is_live
        or cur_status != target_status
        or cur_room_id != target_room_id
        or cur_del != is_account_deleted
    )

    payload: Dict[str, Any] = {
        "is_live": target_is_live,
        "is_recording": target_is_rec,
        "status": target_status,
        "room_id": target_room_id,
        "updated_at": now_iso,
    }

    if is_account_deleted:
        payload["is_account_deleted"] = True
    else:
        if cur_del:
            payload["is_account_deleted"] = False

    if target_is_live:
        payload["last_live_at"] = now_iso

    # Optional metadata enrichments if missing in DB
    avatar_thumb = live_details.get("avatar_thumb")
    if avatar_thumb and not current_row.get("avatar_thumb"):
        payload["avatar_thumb"] = avatar_thumb
        changes_needed = True

    nickname = live_details.get("nickname")
    if nickname and not current_row.get("nickname"):
        payload["nickname"] = nickname
        changes_needed = True

    return payload, changes_needed


def check_streamer_ground_truth(user: str) -> Tuple[str, Dict[str, Any]]:
    """Helper to check single streamer ground truth via recorder_core and direct API fallback with retry."""
    user = user.strip().replace("@", "").lower()
    det = None

    # 1. Primary: recorder_core.check_live_details
    for attempt in range(2):
        try:
            det = recorder_core.check_live_details(user)
            if isinstance(det, dict) and det.get("is_live"):
                return user, det
        except Exception:
            det = None
        if attempt == 0:
            time.sleep(0.2)

    # 2. Secondary fallback: Direct verification with trailing slash and TLS impersonation
    for attempt in range(2):
        try:
            from curl_cffi import requests as c_requests
            cookies = recorder_core.load_cookies()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "Referer": f"https://www.tiktok.com/@{user}/live",
                "Accept": "*/*"
            }
            res = c_requests.get(
                "https://www.tiktok.com/api-live/user/room/",
                params={"aid": 1988, "sourceType": 54, "uniqueId": user},
                headers=headers,
                cookies=cookies,
                impersonate="chrome136",
                timeout=8
            )
            if res.status_code == 200:
                d = res.json()
                data = d.get("data") or {}
                liveRoom = data.get("liveRoom") or {}
                userData = data.get("user") or {}
                st = liveRoom.get("status")
                rid = liveRoom.get("roomId") or userData.get("roomId") or data.get("roomId")
                if st == 2 and rid:
                    return user, {
                        "is_live": True,
                        "room_id": str(rid),
                        "avatar_thumb": userData.get("avatarThumb") or userData.get("avatarMedium"),
                        "nickname": userData.get("nickname"),
                        "is_account_deleted": False,
                    }
                elif st is not None and st != 2:
                    return user, {
                        "is_live": False,
                        "room_id": None,
                        "avatar_thumb": userData.get("avatarThumb") or userData.get("avatarMedium"),
                        "nickname": userData.get("nickname"),
                        "is_account_deleted": False,
                    }
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.3)

    if isinstance(det, dict):
        return user, det

    return user, {
        "is_live": False,
        "room_id": None,
        "is_account_deleted": False,
    }


def patch_streamer_supabase(user: str, payload: Dict[str, Any]) -> bool:
    """Sends a PATCH request to Supabase to update streamer state."""
    headers = supabase_sync.get_supabase_headers()
    url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_streamers?username=eq.{user}"
    try:
        res = requests.patch(url, headers=headers, json=payload, timeout=10)
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[CLEANUP] [!] Error patching @{user} on Supabase: {e}")
        return False


def reconcile_supabase_streamers(
    dry_run: bool = False,
    verbose: bool = False,
    max_workers: int = 6
) -> Dict[str, Any]:
    """
    Main reconciliation logic for Supabase `tiktok_streamers`.
    """
    print(f"\n[CLEANUP] [*] Step 1: Fetching current streamers from Supabase...")
    streamers = fetch_all_streamers_raw()
    print(f"[CLEANUP] [✓] Found {len(streamers)} streamers in Supabase.")

    active_local_users = get_active_local_recordings()
    if active_local_users:
        print(f"[CLEANUP] [*] Detected local active FFmpeg processes: {active_local_users}")
    else:
        print(f"[CLEANUP] [*] No local active FFmpeg recording processes detected.")

    print(f"[CLEANUP] [*] Step 2: Querying TikTok API for ground truth across {len(streamers)} streamers (workers={max_workers})...")
    start_t = time.time()
    ground_truth: Dict[str, Dict[str, Any]] = {}

    usernames = [s["username"].strip().replace("@", "").lower() for s in streamers]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_streamer_ground_truth, u): u for u in usernames}
        for fut in as_completed(futures):
            u, det = fut.result()
            ground_truth[u] = det

    scan_duration = time.time() - start_t
    live_found = sum(1 for det in ground_truth.values() if det.get("is_live"))
    print(f"[CLEANUP] [✓] Ground truth scan complete in {scan_duration:.2f}s. Confirmed Live on TikTok: {live_found}/{len(streamers)}")

    now_iso = datetime.now(timezone.utc).isoformat()
    changes_to_apply: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []

    for s in streamers:
        u = s["username"].strip().replace("@", "").lower()
        det = ground_truth.get(u, {})
        payload, needed = compute_target_state(s, det, active_local_users, now_iso)
        if needed:
            changes_to_apply.append((u, s, payload))

    print(f"\n[CLEANUP] [*] Step 3: Evaluating reconciliation changes...")
    print(f"[CLEANUP] Streamers requiring update: {len(changes_to_apply)}")

    for u, cur, p in changes_to_apply:
        cur_desc = f"live={cur.get('is_live')}, rec={cur.get('is_recording')}, status='{cur.get('status')}', room={cur.get('room_id')}"
        tgt_desc = f"live={p.get('is_live')}, rec={p.get('is_recording')}, status='{p.get('status')}', room={p.get('room_id')}"
        if verbose or cur.get("is_recording") or cur.get("status") == "recording" or p.get("is_live"):
            print(f"  - @{u:22}: [{cur_desc}] --> [{tgt_desc}]")

    applied_count = 0
    if not dry_run and changes_to_apply:
        print(f"\n[CLEANUP] [*] Applying {len(changes_to_apply)} updates to Supabase...")
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_patches = {
                executor.submit(patch_streamer_supabase, u, p): u
                for u, _, p in changes_to_apply
            }
            for fut in as_completed(future_patches):
                u = future_patches[fut]
                if fut.result():
                    applied_count += 1
                else:
                    print(f"[CLEANUP] [!] Failed to update @{u}")
        print(f"[CLEANUP] [✓] Successfully applied {applied_count}/{len(changes_to_apply)} updates to Supabase.")
    elif dry_run:
        print(f"[CLEANUP] [DRY RUN] Skipped writing to Supabase ({len(changes_to_apply)} changes planned).")

    return {
        "total_streamers": len(streamers),
        "live_confirmed": live_found,
        "changes_needed": len(changes_to_apply),
        "changes_applied": applied_count,
        "ground_truth": ground_truth,
        "streamers": streamers,
    }


def cleanup_gdrive_active_recordings(
    ground_truth: Optional[Dict[str, Dict[str, Any]]] = None,
    dry_run: bool = False,
    verbose: bool = False
) -> Dict[str, Any]:
    """
    Cleans up stale test entries and inactive streamers from Google Drive `active_recordings.json`.
    """
    print(f"\n[CLEANUP] [*] Step 4: Inspecting Google Drive active_recordings.json...")
    try:
        drive_details = gdrive_manager.load_active_recordings_from_drive(as_details=True) or []
    except Exception as e:
        print(f"[CLEANUP] [!] Error reading active_recordings.json from Drive: {e}")
        return {"error": str(e), "stale_removed": []}

    print(f"[CLEANUP] Found {len(drive_details)} entries in active_recordings.json on Google Drive.")
    stale_users: Set[str] = set()

    for item in drive_details:
        u = item.get("username", "").strip().replace("@", "").lower()
        if not u:
            continue

        # Condition 1: Explicitly known stale test streamers
        if u in KNOWN_STALE_TEST_STREAMERS:
            stale_users.add(u)
            continue

        # Condition 2: Synthetic test prefixes
        if u.startswith("test_") or u.startswith("acceptance_") or u.startswith("mid_record_"):
            stale_users.add(u)
            continue

        # Condition 3: Ground truth check (if user is confirmed offline on TikTok)
        if ground_truth and u in ground_truth:
            det = ground_truth[u]
            if not det.get("is_live"):
                stale_users.add(u)
                continue

    print(f"[CLEANUP] Identified {len(stale_users)} stale/test/offline entries to remove from Drive: {sorted(list(stale_users))}")

    if not dry_run and stale_users:
        print(f"[CLEANUP] [*] Removing {len(stale_users)} entries from Google Drive active_recordings.json...")
        ok = gdrive_manager.set_users_recording_status_drive(list(stale_users), is_recording=False)
        if ok:
            print(f"[CLEANUP] [✓] Successfully cleaned up Google Drive active_recordings.json!")
        else:
            print(f"[CLEANUP] [!] Warning: Drive update returned False.")
    elif dry_run:
        print(f"[CLEANUP] [DRY RUN] Skipped Google Drive active_recordings update.")

    return {
        "initial_count": len(drive_details),
        "stale_entries": list(stale_users),
        "cleaned": not dry_run and bool(stale_users),
    }


def inspect_and_verify_database(ground_truth: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Queries Supabase and Google Drive to compute verification metrics:
    - Total streamers
    - Live streamers
    - Offline streamers
    - Streamers with is_recording=True
    - Ghost recordings (streamers flagged recording while offline on TikTok or without active process)
    - Offline streamers with lingering room_id (must be 0)
    - Google Drive active recordings count
    """
    streamers = fetch_all_streamers_raw()
    active_local_users = get_active_local_recordings()

    rec_streamers = [s for s in streamers if s.get("is_recording") or s.get("status") == "recording"]
    live_streamers = [s for s in streamers if s.get("is_live") or s.get("status") == "live"]
    offline_streamers = [s for s in streamers if not s.get("is_live") and s.get("status") != "live"]
    lingering_rooms = [s for s in offline_streamers if s.get("room_id") is not None]

    drive_details = []
    try:
        drive_details = gdrive_manager.load_active_recordings_from_drive(as_details=True) or []
    except Exception:
        pass

    now_ts = int(time.time())
    runner_active_users = set()
    for d in drive_details:
        u_name = str(d.get("username", "")).strip().replace("@", "").lower()
        up_at = d.get("updated_at", 0)
        if u_name and (now_ts - up_at < 90):
            runner_active_users.add(u_name)

    # Ghost recordings: streamers flagged recording without active local/runner process or offline
    ghost_recs = []
    for s in rec_streamers:
        u = s["username"].strip().replace("@", "").lower()
        if u not in active_local_users and u not in runner_active_users:
            ghost_recs.append(s)
        elif not s.get("is_live"):
            ghost_recs.append(s)

    stale_in_drive = [
        d["username"] for d in drive_details
        if d.get("username") in KNOWN_STALE_TEST_STREAMERS
        or str(d.get("username")).startswith("test_")
        or str(d.get("username")).startswith("acceptance_")
    ]

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_streamers": len(streamers),
        "live_streamers_count": len(live_streamers),
        "recording_streamers_count": len(rec_streamers),
        "offline_streamers_count": len(offline_streamers),
        "lingering_room_ids_count": len(lingering_rooms),
        "ghost_recordings_count": len(ghost_recs),
        "ghost_recording_users": [s["username"] for s in ghost_recs],
        "drive_active_count": len(drive_details),
        "drive_stale_test_count": len(stale_in_drive),
        "drive_stale_test_users": stale_in_drive,
        "sample_live": [s["username"] for s in live_streamers[:8]],
        "sample_rec": [s["username"] for s in rec_streamers],
    }

    print("\n" + "=" * 65)
    print("           DATABASE & GOOGLE DRIVE VERIFICATION AUDIT")
    print("=" * 65)
    print(f" Timestamp                  : {report['timestamp']}")
    print(f" Total Streamers (Supabase) : {report['total_streamers']}")
    print(f" Live Streamers             : {report['live_streamers_count']}")
    print(f" Offline Streamers          : {report['offline_streamers_count']}")
    print(f" Streamers Flagged REC      : {report['recording_streamers_count']}")
    print(f" Ghost Recordings (Offline+REC): {report['ghost_recordings_count']}")
    if report["ghost_recording_users"]:
        print(f"   Ghost Users              : {report['ghost_recording_users']}")
    print(f" Lingering Offline room_ids : {report['lingering_room_ids_count']}")
    print(f" Google Drive Active Entries: {report['drive_active_count']}")
    print(f" Stale Test Entries in Drive: {report['drive_stale_test_count']}")
    if report["drive_stale_test_users"]:
        print(f"   Stale Drive Users        : {report['drive_stale_test_users']}")
    print("=" * 65 + "\n")

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile Supabase tiktok_streamers & Google Drive active_recordings.json with ground truth."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview planned changes without writing.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed per-streamer diffs.")
    parser.add_argument("--verify-only", action="store_true", help="Only audit and verify database without modifying.")
    args = parser.parse_args()

    print("=" * 65)
    print(" TIKTOK RECORDER MAINTENANCE: GHOST RECORDING CLEANUP (R3)")
    print("=" * 65)

    if args.verify_only:
        inspect_and_verify_database()
        return 0

    print("\n>>> AUDIT BEFORE CLEANUP:")
    report_before = inspect_and_verify_database()

    reconcile_res = reconcile_supabase_streamers(dry_run=args.dry_run, verbose=args.verbose)
    drive_res = cleanup_gdrive_active_recordings(
        ground_truth=reconcile_res.get("ground_truth"),
        dry_run=args.dry_run,
        verbose=args.verbose
    )

    print("\n>>> AUDIT AFTER CLEANUP:")
    report_after = inspect_and_verify_database(ground_truth=reconcile_res.get("ground_truth"))

    print("\n" + "=" * 65)
    print("                   CLEANUP SUMMARY REPORT")
    print("=" * 65)
    print(f" Ghost recordings before : {report_before['ghost_recordings_count']}")
    print(f" Ghost recordings after  : {report_after['ghost_recordings_count']}")
    print(f" Lingering room_id before: {report_before['lingering_room_ids_count']}")
    print(f" Lingering room_id after : {report_after['lingering_room_ids_count']}")
    print(f" Stale Drive test entries: {report_before['drive_stale_test_count']} -> {report_after['drive_stale_test_count']}")
    print("=" * 65)

    if report_after["ghost_recordings_count"] == 0 and report_after["lingering_room_ids_count"] == 0:
        print("[✓] ALL ACCEPTANCE CRITERIA MET: Database and Drive are clean and consistent!\n")
        return 0
    elif args.dry_run:
        print("[*] Dry run completed.\n")
        return 0
    else:
        print("[!] Warning: Some inconsistencies remain. Check details above.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
