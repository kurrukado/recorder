# Project: TikTok Recorder & Web-Truyen Synchronization & Cloud 24/7 Fix

## Architecture
The system consists of two primary subsystems operating under the "Cloud 24/7" rule:
1. **Frontend / Edge Proxy (`D:\web-truyen`)**:
   - Next.js 14 App Router application hosted on Vercel / Edge.
   - API Route Proxy (`app/api/record/[...path]/route.js`): Proxies requests to local recorder tunnels or falls back to Supabase Cloud 24/7 mode (`handleSupabaseGetFallback`, `handleSupabasePostFallback`). Interacts directly with TikTok Native Live API (`/api-live/user/room/`) for avatar, live room, and status checking.
   - Web UI (`app/record/page.js`): Real-time streamer dashboard displaying status badges (`🔴 REC`, `🟢 LIVE`, `Offline`), stream preview player, recordings list, and add/delete actions.
2. **Backend Recorder & Cloud Daemon (`d:\recorder`)**:
   - Cloud daemon (`cloud_daemon.py`) running in GitHub Actions and/or local daemon.
   - Core recorder engine (`recorder_core.py`): FFmpeg video capture, segment chunking, and heartbeat maintenance.
   - Supabase sync helper (`supabase_sync.py`): PostgREST API sync for streamer metadata (`tiktok_streamers`) and video catalog (`tiktok_recordings`).
   - Storage: Google Drive (raw 5TB archive) and Supabase Storage `covers` bucket (public thumbnail CDN).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Strict TikTok Live Parsing in Route Proxy | Ensure `isLive = liveRoom?.status === 2 && Boolean(roomId)`. Never treat `userData?.roomId` as live when `liveRoom` is missing/ended. | M2 | Survey Explorer 1 |
| 2 | Speculative Recording Removal | `POST /record/start` must only set `is_live: true, status: 'live'`. Real `is_recording: true, status: 'recording'` is set only by active FFmpeg worker. | M2 | Survey Explorer 1 |
| 3 | Refined Stale Guard | 10-minute inactivity guard demotes only `is_recording = false`, preserving `is_live` unless verified offline by TikTok API. | M2 | Survey Explorer 1 |
| 4 | Web UI Live Status Polling | Periodic background live check (< 15s discovery) for all active streamers in `page.js`. | M2 | Survey Explorer 1 |
| 5 | Web UI Accurate Toast & Order Stability | Show "đang phát Live" instead of "đang ghi hình"; maintain strict newest-first sorting (`sortStreamersFixed`). | M2 | Survey Explorer 1 |
| 6 | Database Ghost Recording Remediation | Reset stuck `is_recording = true` flags and sync `is_live` and `room_id` with TikTok reality for all rows in `tiktok_streamers`. | M1 | Survey Explorer 3 |
| 7 | Google Drive Artifact Cleanup | Clean stale/test handles from `active_recordings.json`. | M1 | Survey Explorer 3 |
| 8 | Room ID Reset on Offline | `update_streamer_status_supabase` resets `room_id = None` when transitioning to offline. | M3 | Survey Explorer 2 |
| 9 | Worker Lock & Abort Lifecycle Safety | If recorder thread cannot acquire file lock or exits early, prune from `ACTIVE_RECORDERS` and reset Supabase status. | M3 | Survey Explorer 2 |
| 10 | Worker Termination & Stale Check Fix | Prevent race condition in worker `finally:` block where daemon dead thread pruning caused status cleanup to be skipped. | M3 | Survey Explorer 2 |
| 11 | Disconnect & Upload Status Integrity | Do not assert `is_recording: true` during 5-minute disconnect wait or Google Drive upload when offline confirmed. | M3 | Survey Explorer 2 |
| 12 | Heartbeat Tightening | Pulse active FFmpeg recording heartbeat every 30s instead of 60s in `recorder_core.py`. | M3 | Survey Explorer 2 |
| 13 | E2E Acceptance & Audit Suite Pass | Ensure 100% pass on `test_audit_suite.py` and `test_milestone4_e2e_acceptance.py`. | M4 | Survey Explorer 2 |
| 14 | Frontend Build Verification | Ensure `npm run build` exits 0 on `D:\web-truyen`. | M4 | Survey Explorer 1 |
| 15 | Forensic Integrity Verification | Audit against hardcoded values, dummy stubs, and verify genuine end-to-end sync logic. | M5 | Forensic Auditor |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Data Management & Supabase Cleanup (R3) | Reconcile `tiktok_streamers` on Supabase; clear ghost recordings; clean Google Drive `active_recordings.json`. | None | DONE (cleanup_ghost_recordings.py verified, 0 ghost recordings, 114/114 tests pass) |
| M2 | Frontend API Proxy & Web UI (R1, R2) | Sửa `route.js` và `page.js` trên `D:\web-truyen` theo quy tắc nhận diện live nghiêm ngặt, khử speculative recording, chỉnh stale guard, và thêm live polling. | None | DONE (npm run build exit 0, 100% tests pass) |
| M3 | Backend Heartbeat & Lifecycle (R4) | Sửa `supabase_sync.py`, `cloud_daemon.py`, và `recorder_core.py` trên `d:\recorder` cho heartbeat và lifecycle sạch. | None | DONE (30s heartbeat, room_id=None on offline, 113/113 tests pass) |
| M4 | QA, Build & Adversarial Review | Chạy `npm run build` trên `D:\web-truyen`, chạy unittest suites trên `d:\recorder`, kiểm tra các ca biên. | M1, M2, M3 | DONE (Reviewers & Challenger APPROVED, npm run build exit 0, 100% tests pass) |
| M5 | Forensic Integrity Audit | Kiểm định độc lập toàn diện tính toàn vẹn của mã nguồn và dữ liệu. | M4 | DONE (Auditor verdict CLEAN, 0 integrity violations, 0 facades) |

## Interface Contracts
### Next.js API Proxy (`route.js`) ↔ TikTok API (`/api-live/user/room/`)
- Request: `GET https://www.tiktok.com/api-live/user/room/?uniqueId={username}&sourceType=54` with User-Agent & headers.
- Response Parsing:
  - `isLive = liveRoom?.status === 2 && Boolean(roomId)`.
  - `roomId = liveRoom?.roomId || data?.data?.roomId || liveRoom?.streamId || (liveRoom?.status === 2 ? userData?.roomId : null)`.
  - When `liveRoom?.status !== 2`: `isLive = false`, `roomId = null`.

### Next.js API Proxy (`route.js`) ↔ Supabase (`tiktok_streamers`)
- In `record/start`:
  - When streamer is live: `{ is_live: true, status: 'live', room_id: roomId, updated_at: now() }`.
  - When streamer is offline: `{ is_live: false, is_recording: false, status: 'offline', room_id: null, updated_at: now() }`.
- In `handleSupabaseGetFallback`:
  - `isStale = (now - updatedAtMs) > 10 * 60 * 1000`.
  - `isRecording = !isDeleted && Boolean(s.is_recording || s.status === 'recording') && !isStale`.
  - `isLive = !isDeleted && Boolean(s.is_live || s.status === 'live' || isRecording)`.

### Backend Recorder (`cloud_daemon.py` / `recorder_core.py`) ↔ Supabase (`supabase_sync.py`)
- Active recording heartbeat: `update_streamer_status_supabase(user, is_live=True, is_recording=True, status="recording")` every 30 seconds.
- Offline transition: `update_streamer_status_supabase(user, is_live=False, is_recording=False, status="offline")` sets `payload["room_id"] = None`.
- Deletion Guard: NEVER call status sync when `getattr(stop_event, "user_deleted", False) == True`.

## Code Layout
- `D:\web-truyen\app\api\record\[...path]\route.js` (Owned by Worker Frontend)
- `D:\web-truyen\app\record\page.js` (Owned by Worker Frontend)
- `d:\recorder\cleanup_ghost_recordings.py` (Owned by Worker Data)
- `d:\recorder\supabase_sync.py` (Owned by Worker Backend)
- `d:\recorder\cloud_daemon.py` (Owned by Worker Backend)
- `d:\recorder\recorder_core.py` (Owned by Worker Backend)
