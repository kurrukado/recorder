# TikTok 24/7 Auto Recorder — Cloud Multi-Stream Engine

> **Ghi hình TikTok 24/7 trên cloud miễn phí**: GitHub Actions ghi hình (tối đa 10 streamer song song),
> Google Drive 5TB lưu trữ, Supabase giữ metadata cho web, Render/Vercel phục vụ API + giao diện.
> **Yêu cầu cốt lõi: hệ thống chạy không cần máy tính local.** Mọi khâu vận hành (ghi hình, lưu trữ,
> API, web) đều nằm trên cloud; máy cá nhân chỉ là **tuỳ chọn** khi phát triển / ghi cục bộ.

---

## 1. Kiến trúc tổng quan

```text
 [Supabase tiktok_streamers] ─┐
 [Drive streamers.json]      ─┼─► load_monitored_users()   (cache 15s, gộp 3 nguồn)
 [config.json monitored_users]┘            │
                                           ▼
                  run_daemon()  poll ~20s (jitter), tối đa 10 worker
                    │  chống trùng:  heartbeat Drive (TTL <180s)
                    │              + .locks/<user>.recording.lock
                    │              + cooldown lũy tiến 30s → 480s
                    ▼
        streamer_recording_worker(user)          — 1 thread / streamer
          check_live → get_stream_candidates (ưu tiên HLS, fallback FLV)
          → record_stream_ffmpeg (`-c copy`, ≤3600s/part) → concat
                    ▼
        auto_h264: sanitize bitstream + faststart + upscale 1080p + thumbnail giữa video
                    ▼
        upload TRỰC TIẾP mọi segment → Drive tiktok-record/<user>/ + Supabase
                    ▼
        web-truyen: /record ─► proxy /api/record/* ─► API :8000 / Render
                    phát video ─► stream-video-id ─► 302 Google Edge CDN (0 bandwidth)
```

**Trục dữ liệu:** Drive/Supabase (danh sách) → daemon/API (điều phối) → recorder_core (ghi) →
auto_h264 (chuẩn hóa) → Drive + Supabase (lưu) → web-truyen (hiển thị / phát).

**Đặc điểm chính:** cloud-first 24/7 · không re-encode video (`-c copy`) · tự lành lỗi 6 lần/ngày ·
phát video 0 bandwidth qua CDN · 10 streamer song song · tự tách part khi đổi độ phân giải.

**Phân bổ hạ tầng — không khâu nào bắt buộc máy local:**

| Thành phần | Chạy trên (cloud) | Cần máy local? |
| :--- | :--- | :--- |
| Ghi hình 24/7 | GitHub Actions (`recorder.yml`) | Không |
| Sửa video lỗi | GitHub Actions (`repair-watchdog.yml`) | Không |
| Lưu trữ video | Google Drive (5TB) | Không |
| Metadata / heartbeat | Supabase | Không |
| REST API | Render Free (`tiktok-recorder-api`) | Không |
| Web UI | Vercel | Không |
| Chạy thử / ghi cục bộ (`run_*.bat`, CLI, API `:8000`) | Windows + FFmpeg | **Tuỳ chọn (chỉ khi phát triển)** |

---

## 2. Cấu trúc dự án

```text
D:\tiktok-recorder\
├── .github/workflows/
│   ├── recorder.yml              # Ghi hình 24/7 (cron */50 + tự relay phiên kế tiếp)
│   ├── repair-watchdog.yml       # Tự lành 6 lần/ngày
│   └── ci-tests.yml              # Lint (flake8 E9,F63,F7,F82) + test_audit_suite.py + smoke test
├── cloud_daemon.py               # Orchestrator đa luồng (10 streamer)
├── recorder_core.py              # Lõi phát hiện live TikTok + FFmpeg recorder
├── auto_h264.py                  # Sanitize bitstream, upscale 1080p, thumbnail
├── gdrive_manager.py / gdrive_auth.py   # Google Drive API v3 + OAuth2
├── supabase_sync.py              # PostgREST + Storage client
├── api_server.py                 # FastAPI REST API (:8000)
├── repair_cloud_videos_engine.py # Sửa video lỗi trên Drive
├── staging_queue.py              # Hàng đợi cũ (không còn dùng)
├── constants.py / shared_config.py / logging_utils.py / config_lock.py
├── notifier.py                   # Telegram + sync file lên Drive
├── tiktok_recorder.py            # CLI menu
├── config.json (gitignored) / config.example.json
├── requirements.txt
├── run_cloud_daemon.bat / run_recorder.bat   # tuỳ chọn: chạy local khi phát triển
├── test_audit_suite.py           # Bộ test hồi quy (CI chạy bộ này)
├── README.md / HD_SU_DUNG.md
└── project.md                    # Tài liệu này
```

`constants.py` là **nguồn duy nhất** cho `BASE_DIR` / `CONFIG_FILE`; mọi module khác đều lấy từ đây
(không tự suy diễn `os.path.dirname(__file__)`).

**Phụ thuộc** (`requirements.txt`): FastAPI / uvicorn / pydantic (API), `curl_cffi` (TLS-impersonate
Chrome khi gọi TikTok), `requests` / `httpx` / `aiohttp` (HTTP), `imageio-ffmpeg`, `psutil` (giám sát
RAM), `aiofiles`, `asyncio-throttle`, `cachetools`, `python-multipart`.
FFmpeg tìm theo thứ tự: `ffmpeg.exe` cạnh source → `shutil.which("ffmpeg")` →
`imageio_ffmpeg.get_ffmpeg_exe()` → `/usr/bin/ffmpeg`.

---

## 3. Pipeline ghi hình end-to-end

1. **Nạp danh sách** — `load_monitored_users()` (cache 15s) gộp Drive `streamers.json` + Supabase +
   `config.json`; nếu cả hai nguồn trả rỗng **hợp lệ** thì dùng rỗng (không giữ cache cũ → không xoá nhầm).
2. **Phát hiện live** — `check_live_details()` trả `{is_live, room_id, is_sub_only, is_preview,
   paid_type, preview_duration, …}`, timeout 6s; cache 60s ở API (`LIVE_CACHE`).
3. **Tự khám phá** — `discover_new_streamers()` quét HTML trang live (curl_cffi impersonate chrome136),
   tự thêm đối thủ PK/co-host vào danh sách.
4. **Chọn luồng** — `get_stream_candidates()` + `classify_stream_urls()`: ưu tiên HLS/H.264, xếp hạng
   theo codec/bandwidth (Tier-1 = 1080p/`_or4`/origin), fallback FLV; thử tối đa 4 ứng viên khi ghi.
5. **Ghi** — `record_stream_ffmpeg()`: `-c copy`, tối đa 3600s/part; watchdog chốt hết live sau ≥2 lần
   kiểm tra offline liên tiếp; dừng mềm `_safe_stop_ffmpeg()` để FFmpeg ghi `moov atom`.
6. **Chuẩn hóa** — `validate_playable_video()` (≥5s, ≥250KB) → `ensure_h264()` →
   `sanitize_mp4_bitstream()` → `has_faststart()` → `upscale_to_1080p_if_needed()` →
   `extract_middle_thumbnail()` (khung giữa video).
7. **Upload trực tiếp** — mọi segment được upload ngay lên Drive `tiktok-record/<user>/` + Supabase;
   segment đủ 1h → upload rồi ghi phần mới; segment cuối → upload sau khi xác nhận offline
   (`offline_confirm_seconds=300`). Không dùng staging queue.
8. **Xoay phiên (DRAIN)** — hết `--duration-minutes` → không khởi tạo luồng mới, chờ
   `DRAIN_GRACE_SECONDS=360s` để upload hoàn tất.
9. **Chống trùng** — heartbeat `active_recordings.json` trên Drive (runner khác thấy <180s thì bỏ qua)
   + `streamer_recording_lock()` chặn 2 process cùng ghi 1 user.

---

## 4. Giải thích module

### 4.1 `cloud_daemon.py` — bộ điều phối
* `load_monitored_users()` — gộp Drive + Supabase + config, cache 15s.
* `discover_new_streamers()` — quét HTML tìm PK/co-host.
* `streamer_recording_worker()` — vòng đời 1 streamer: nhận lock, tạo folder Drive, xử lý VIP Sub-Only
  (guest session, preview ≤300s), thu part đến 3600s, chờ xác nhận offline (300s, recheck 15s); khi
  **chưa thu được giây nào** (`accumulated_seconds < 15`) thì thoát ngay (xem 9.1).
* `run_daemon()` — poll (`--duration-minutes` mặc định 210, `--interval` 25s),
  `MAX_CONCURRENT_RECORDERS=10`, gửi Telegram khi phát hiện live, cooldown lũy tiến + bỏ qua streamer
  "độc" (xem 9.2), GC cooldown định kỳ.
* Worker upload trực tiếp mọi segment (không qua staging queue); row Supabase chỉ tạo khi có `drive_file_id`.

### 4.2 `recorder_core.py` — lõi trích xuất
* `load_cookies()` — **luôn** khởi tạo `cookies = {}` (tránh `UnboundLocalError`), tìm cookie đa đường
  dẫn (`cookies.json` → `web-truyen/kuruRecord/cookies.json` → env `TIKTOK_SESSION_ID` → config);
  luôn gắn `tt-target-idc`.
* `check_live_details()` / `check_live_status()` / `check_user_live()` — Native API
  `https://www.tiktok.com/api-live/user/room` (không trailing slash) với
  `aid=1988, sourceType=54, staleTime=600000` và header `Referer: …/{user}/live`; kiểm tra cả
  `streamData` và `hevcStreamData`; fallback scrape HTML + `urllib.request`.
* `get_stream_urls()` / `parse_sdk_stream_data()` — giải mã JSON SDK của TikTok.
* `record_stream_ffmpeg()` — ghi `-c copy`. Cờ mạng tách theo giao thức: HLS (`.m3u8`) **không** dùng
  `-reconnect_at_eof`/`-reconnect_streamed` (gây đóng băng 0.00 MB); FLV giữ đầy đủ. Khởi động nhanh:
  `-analyzeduration` 3s, `-probesize` 2MB. Quản lý process qua `GLOBAL_RECORDING_PROCS` + `atexit`.
* `concat_mp4_segments()` — concat demuxer `-c copy`, timeout 180s, bỏ segment <50KB; probe đầu/giữa/cuối
  để cảnh báo đổi độ phân giải giữa chừng.
* `DEFAULT_CONFIG` — `check_interval_seconds=20`, `max_recording_seconds=3600`,
  `offline_confirm_seconds=300`, `timezone_offset_hours=7`, `auto_upscale_1080p=true`.

### 4.3 `auto_h264.py` — chuẩn hóa H.264
`get_best_h264_encoder()` (NVENC → QSV → AMF → MF → libx264), `get_video_codec()`,
`validate_playable_video()`, `check_h264_stream_health()`, `sanitize_mp4_bitstream()`,
`ensure_h264()`, `upscale_to_1080p_if_needed()`, `extract_middle_thumbnail()`, `get_video_duration()`.

**Chống hỏng khung hình (khối xám + nhiễu macroblock):**

| Vấn đề | Cách xử lý hiện tại |
|---|---|
| Quét lỗi quá hẹp | `_build_scan_windows()` quét 3 cửa sổ (đầu / 40% / 75% thời lượng, mỗi cửa sổ 4s, keyframe seek); file <90s chỉ quét cửa sổ đầu |
| Thiếu pattern lỗi MB | `_FATAL_PATTERNS` (hỏng triệt để) + `_MB_ERROR_PATTERNS` (`error while decoding MB`, `concealing`, `reference picture missing`, …) |
| Chẩn đoán nhầm | Ngưỡng `_MB_ERROR_THRESHOLD=40` lỗi/cửa sổ; kết luận hỏng khi ≥2 cửa sổ vượt ngưỡng **hoặc** 1 cửa sổ ≥4× ngưỡng (tránh xoá nhầm video đã ghi tốt) |
| Cắt cụt SPS/PPS | Đọc avcC tới 4096 byte + kiểm tra biên từng NAL; thiếu header thì để trống và rơi vào nhánh remux/transcode an toàn |
| Sai `lengthSizeMinusOne` | `_parse_avcc_record()` trả `nalu_len_bytes`; `_write_length_prefixed_nals()` dùng đúng độ dài; parse hỏng thì bỏ mẫu |
| Box walker lỗi | `sz < 8` / box không hợp lệ → `break` (không seek lùi vô hạn) |
| Audio gating sai | `ensure_h264()` lấy sức khỏe **bitstream video** làm chuẩn; audio khác AAC → remux (giữ nguyên video, chỉ transcode audio) |

### 4.4 `gdrive_manager.py` + `gdrive_auth.py`
* `get_access_token()` — cache token, tự refresh, tự làm mới khi 401.
* `find_or_create_folder()` (cache), `upload_file_to_drive()` — resumable chunk 5MB, retry 5 lần/chunk,
  chặn MP4 <250KB.
* `is_recorder_owned_file()` — xác minh `file_id` nằm trong `tiktok-record/` (≤8 cấp cha, cache 600s);
  **bắt buộc** trước mọi thao tác public/redirect.
* `make_file_public()` → `get_cdn_download_url()`.
* `load_streamers_from_drive()` / `save_streamers_to_drive()` / `load_active_recordings_from_drive()` +
  `set_users_recording_status_drive()` (heartbeat, có điều tiết trùng lặp — xem 9.3) /
  `set_user_recording_status_drive()` / `delete_file_drive()` / `clean_corrupt_files_from_drive()`.
* `gdrive_auth.py` — OAuth2 flow `localhost:8080` nhận code, chờ 300s (**chạy 1 lần** để lấy refresh token, sau đó token nằm trong GitHub Secrets — không cần máy local khi vận hành).

### 4.5 `supabase_sync.py` — đồng bộ dữ liệu cho web
* `_secret_from_config()` — đọc env trước, fallback `config.json` (**không hardcode key trong source**).
* `fetch_streamers_from_supabase()`, `add_streamer_to_supabase()` (upsert),
  `fetch_streamers_details_from_supabase()`, `update_streamer_status_supabase()`,
  `get_active_recordings_from_supabase()`.
* `upload_thumbnail_to_supabase()` — nhận path/bytes/URL/`gdrive:<id>`, upsert vào
  `covers/record-thumbnails/{user}/{name}.jpg`.
* `sync_recording_to_supabase()` — upsert `tiktok_recordings` theo `on_conflict=filename`; bỏ qua video
  <250KB; không ghi đè `thumbnail_url` cũ; chỉ tạo row khi upload Drive thành công.
* `delete_streamer_data_supabase()`.

### 4.6 `api_server.py` — REST API (xem mục 5)
* CORS `allow_origins=["*"]` + middleware **PIN opt-in** cho POST/PUT/PATCH/DELETE (GET luôn công khai).
* Cache: live `LIVE_CACHE_TTL=60s` (offline `LIVE_CACHE_OFFLINE_TTL=15s`), recordings 60s.
* `bg_record_worker()` — bản sao worker chạy trong process API (3600s/part, heartbeat 60s, offline
  5 phút, cùng guard `MIN_OFFLINE_CONFIRM_SECONDS=15`), `source="api_server"`.
* `get_users(fresh=true)` / `get_user_live_details_cached(force_refresh=true)` — bỏ cache khi người dùng
  bấm "Làm mới".
* `clean_zombie_recordings()` — dọn task zombie sau 10 phút không heartbeat. Dọn RAM khi RSS >300MB,
  cảnh báo >350MB, trần `render_limit_mb=512`.

### 4.7 `repair_cloud_videos_engine.py` — tự lành
`process_single_repair()` (tải → sửa bitstream → patch lại Drive → cập nhật Supabase),
`run_repair_engine()` (cờ `--today` = cửa sổ 28h, `--priority-only` = lỗi `NAL_UNIT_SIZE_ERROR`,
`--max-videos=N` mặc định `DEFAULT_MAX_VIDEOS=40`; duyệt `order=id.desc`). Tiến trình
`cloud_repair_progress.json` (`repaired_ids`/`failed_ids`, `MAX_REPAIRED_HISTORY=5000`) lưu **cả cục bộ
lẫn trên Drive** rồi hợp nhất (xem 9.4); retry ≤3.

### 4.8 `staging_queue.py` — **ngừng sử dụng**
Mọi segment upload trực tiếp lên Drive. `api_server.py` không còn gọi module này; `cloud_daemon.py` chỉ
còn 1 lời gọi `check_and_flush_idle_queues()` để xả hàng đợi legacy trên Drive. Xả hết thì có thể xoá module.

### 4.9 Tiện ích
* `notifier.py` — `send_telegram()` (cần `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), `sync_to_gdrive()`.
* `config_lock.py` — `config_transaction()` (khóa `config.lock`, thread RLock + file lock `msvcrt`/`fcntl`),
  `streamer_recording_lock(user)` (`.locks/<user>.recording.lock`, non-blocking).
* `tiktok_recorder.py` — CLI: `--auto`, `--now`, `--status`, `--set-cookie`, `--convert-h264`,
  `--set-vps-cloud`, `--interval`.

---

## 5. REST API (FastAPI :8000)

Khởi động: `python api_server.py` → `uvicorn` lắng nghe `0.0.0.0:8000`. `/api/version` trả
`{"version":"2.2.0","build":"native-safari-tls-sync"}`.

| Method | Endpoint | Chức năng |
| :--- | :--- | :--- |
| GET | `/api/health` | `{status, time, active_recordings[], active_count}` |
| GET | `/api/recordings/active` | Streamer đang ghi thật (Drive heartbeat + task local, dọn zombie trước) |
| GET | `/api/users?check_live=true&fresh=false` | Danh sách user + trạng thái live/đang ghi |
| POST | `/api/users` | Thêm streamer (config, Drive, Supabase); tự kiểm tra live + ghi ngay |
| DELETE | `/api/users/{username}?delete_files=true` | Xóa streamer + (tuỳ chọn) file/DB |
| GET | `/api/memory` | Giám sát RAM/swap, purge cache nếu RSS >300MB |
| GET | `/api/stream/{username}` | URL stream + thông tin live |
| GET | `/api/version` | Version/build |
| GET | `/api/test-live/{username}` | Chẩn đoán: room_id, codec, danh sách URL |
| POST | `/api/record` (= `/api/record/start`) | Thêm user và bắt đầu ghi ngay (background worker) |
| POST | `/api/record/stop` | Dừng ghi an toàn (ghi `moov`) |
| GET | `/api/recordings` | Gộp Drive + local (cache 60s) |
| POST | `/api/recordings/sync-supabase` | Đẩy toàn bộ video + thumbnail lên Supabase |
| GET | `/api/thumbnail/{user}/{filename}?redirect=true` | Ảnh giữa video; `redirect=true` → 302 CDN |
| GET | `/api/download/{user}/{filename}` | Link tải CDN tốc độ cao (Range/IDM) |
| GET | `/api/cdn/{user}/{filename}` | URL Google Edge CDN |
| GET | `/api/stream-video-id/{file_id}?redirect=true` | 302 → CDN (phát HTML5, 0 bandwidth); chặn `file_id` ngoài `tiktok-record/` → 403 |
| GET | `/api/stream-video/{user}/{filename}` | Phát theo user/filename (chặn path traversal) |
| POST | `/api/gdrive/sync` | Đẩy local → Drive |

Body models: `AddUserRequest {username}`, `RecordRequest {username, duration_seconds?}`.
Mọi POST/PUT/PATCH/DELETE đi qua middleware PIN nếu đã đặt env `RECORD_PIN` **hoặc** `config.json:api_pin`;
client gửi header `x-record-pin` hoặc `Authorization: Bearer <pin>`.

---

## 6. Cấu hình & biến môi trường

### `config.json` (mẫu: `config.example.json`)
| Khóa | Ý nghĩa |
| :--- | :--- |
| `target_user` | User mặc định cho CLI |
| `monitored_users[]` | Danh sách cục bộ (gộp cùng Drive + Supabase) |
| `check_interval_seconds` | Chu kỳ poll (20) |
| `quality` | `best` |
| `output_dir` | Thư mục xuất (`.`) |
| `auto_upscale_1080p` | Tự upscale lên 1080p (true) |
| `max_recording_seconds` | Giới hạn mỗi đoạn (3600) |
| `offline_confirm_seconds` | Chờ xác nhận offline (300) |
| `timezone_offset_hours` | Múi giờ đặt tên file (7) |
| `google_client_id` / `google_client_secret` / `gdrive_refresh_token` / `gdrive_enabled` | Cấu hình Google Drive |
| `supabase_key` | Anon key (fallback khi không có env `SUPABASE_*`) |
| `api_pin` | PIN cho POST/PUT/PATCH/DELETE; để rỗng = tắt auth |

### Secrets / env (GitHub Secrets hoặc biến hệ thống)
| Tên | Bắt buộc | Dùng ở đâu |
| :--- | :--- | :--- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` | ✔ | `gdrive_manager.py`; workflow ghi vào `config.json` |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` (hoặc `SUPABASE_KEY`) | ✔ | `supabase_sync.py` (env trước, fallback config) |
| `RECORD_PIN` | ✖ | Bật PIN cho API (tương đương `config.json:api_pin`) |
| `TIKTOK_SESSION_ID` | ✖ | Ghi vào `cookies.json` → live 18+/VIP |
| `TZ`, `TZ_OFFSET_HOURS` | ✖ | Workflow đặt `Asia/Ho_Chi_Minh` + `7` |
| `WORKFLOW_PAT` / `GH_PAT` / `GITHUB_TOKEN` | ✖ | Relay phiên kế tiếp của `recorder.yml` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | ✖ | `notifier.py` |

`.gitignore` loại `config.json`, `cookies.json`, `*.lock`, và mọi file media.

---

## 7. Lưu trữ: Google Drive + Supabase

```text
tiktok-record/
├── streamers.json                 # Danh sách streamer (nguồn dự phòng)
├── active_recordings.json         # Heartbeat "đang quay" → chống trùng runner
├── cloud_repair_progress.json     # Tiến trình tự lành (bền vững giữa các runner)
└── <username>/
    ├── <username>_YYYY-MM-DD_HH-MM-SS.mp4
    └── <username>_YYYY-MM-DD_HH-MM-SS.jpg
```
URL phát: `https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t`

**Supabase**
* `tiktok_streamers`: `id`, `username` (unique), `created_at`, `is_live`, `is_recording`, `room_id`,
  `nickname`, `avatar_thumb`, `status`, `last_live_at`.
* `tiktok_recordings` (upsert theo `filename`): `filename`, `username`, `size_bytes`, `size_mb`,
  `recorded_at`, `created_at`, `thumbnail_url`, `download_url`, `cdn_download_url`, `drive_file_id`,
  `drive_thumb_id`, `source`.
* Storage bucket `covers`: `record-thumbnails/{username}/{basename}.jpg`.

---

## 8. GitHub Actions & triển khai

### `recorder.yml` — "TikTok 24-7 Auto Recorder"
* Kích hoạt `workflow_dispatch` + `cron: '*/50 * * * *'`; `concurrency: tiktok-recorder-runner`
  (`cancel-in-progress: false`); `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`; job
  `timeout-minutes: 210`.
* Bước: checkout `@v7` → Python 3.11 `@v7` → `apt install ffmpeg` → `pip install -r requirements.txt`
  → ghi secrets vào `config.json`/`cookies.json` → `python -u cloud_daemon.py --duration-minutes 170
  --interval 20` → bước cuối `if: always()` POST `…/recorder.yml/dispatches` để chạy phiên kế tiếp
  **ngay lập tức** (không PAT → fallback cron 50 phút).

### `repair-watchdog.yml` — "Kuru Record Self-Healing Watchdog"
`cron: '0 1,5,9,13,17,21 * * *'` (6 lần/ngày), `timeout-minutes: 20` →
`python repair_cloud_videos_engine.py --today` (tiến trình lưu trên Drive nên chỉ tải video mới).

### `ci-tests.yml` — kiểm tra hồi quy
`flake8 . --select=E9,F63,F7,F82` + `python -m unittest test_audit_suite.py` + smoke test API Render.

### Render / Vercel
`render.yaml` chỉ khai báo web service `tiktok-recorder-api` (`uvicorn api_server:app`, health
`/api/health`). Cron `tiktok-monitor` đã **vô hiệu hóa** (xem 9.5). Vercel giữ nguyên nguyên tắc
0-byte proxy.

---

## 9. Cơ chế tiết kiệm tài nguyên & tự chữa lành

Hệ thống chạy trên 3 hạ tầng miễn phí: GitHub Actions (ghi hình 24/7), Google Drive 5TB (lưu trữ),
Render Free 512MB + Vercel Free (API/proxy). Các ngưỡng dưới đây là hằng số trong mã nguồn.

### 9.1 Xác nhận offline thông minh (`MIN_OFFLINE_CONFIRM_SECONDS = 15`)
* Khi phiên ghi khởi động nhưng **chưa thu được giây dữ liệu nào** (`accumulated_seconds < 15`),
  bot **bỏ qua vòng chờ xác nhận offline 300s** và thoát worker ngay.
* Nếu vẫn ngủ đủ `offline_confirm_seconds`, bot vừa gọi heartbeat Drive mỗi 15s vừa giữ slot ghi mà
  không sản sinh được gì.
* Chỉ khi **đã có dữ liệu** mới vào vòng chờ xác nhận offline. Áp dụng ở cả `cloud_daemon.py`
  (`streamer_recording_worker`) và `api_server.py` (`bg_record_worker`).

### 9.2 Chống hot-loop: cooldown lũy tiến & bỏ qua streamer "độc"
* `run_daemon` theo dõi kết quả mỗi worker qua `_WORKER_OUTCOME` (`produced` / `stopped`).
* Streamer liên tục tắt sau khi khởi động mà **không sản sinh video** bị giãn cách thử lại theo
  `_USER_FAILURE_STREAK`: `USER_START_COOLDOWN_SECONDS=30s → 60s → 120s → 240s`, chặn trên
  `MAX_START_COOLDOWN_SECONDS=480s`.
* Sau `MAX_USER_SESSION_FAILURES=3` phiên liên tiếp 0 giây, streamer vào `_SESSION_SKIP_USERS`: tạm bỏ
  qua tới hết phiên daemon hiện tại (tự reset ở runner mới) để nhường slot/CPU cho streamer thật.
* Vòng GC định kỳ xoá mốc `_USER_START_COOLDOWN` cũ hơn `MAX_START_COOLDOWN_SECONDS*2`.

### 9.3 Điều tiết heartbeat Drive (`_HEARTBEAT_MIN_INTERVAL = 30s`)
* `set_users_recording_status_drive()` bỏ qua các lần ghi `is_recording=True` **trùng lặp trong 30s**
  (nhỏ hơn TTL dedup 45s của `active_recordings.json`).
* Ghi `is_recording=False` (kết thúc) **luôn được thực hiện**, không bao giờ bị throttle.

### 9.4 Tiến trình tự-chữa-lành bền vững trên Google Drive
* Runner GitHub Actions là ephemeral nên `cloud_repair_progress.json` cục bộ không tồn tại giữa các
  lần chạy. Engine lưu/đọc thêm bản trên Drive (`tiktok-record/cloud_repair_progress.json`) và **hợp
  nhất** hai nguồn khi khởi động.
* Nhờ đó mỗi lần watchdog chỉ tải và kiểm tra video **mới / chưa từng kiểm tra** thay vì tải lại toàn bộ.
* Trần xử lý/lần chạy: `DEFAULT_MAX_VIDEOS=40` (cờ `--max-videos=N`); duyệt `order=id.desc`; lịch sử
  `repaired_ids` cắt còn `MAX_REPAIRED_HISTORY=5000`.

### 9.5 Cấu hình Render Free / Vercel Free
* `render.yaml` chỉ khai báo web service `tiktok-recorder-api`. Cron `tiktok-monitor` (từng chạy
  `cloud_daemon.py` mỗi 5 phút) đã **vô hiệu hóa kèm chú thích**: Render Free không có Cron Jobs, và
  nếu bật sẽ tạo nhiều daemon ghi hình chồng nhau. Việc ghi 24/7 do `recorder.yml` đảm nhiệm.
* Vercel Free giữ nguyên tắc **0-byte proxy**: mọi URL `video/*` redirect thẳng tới Drive/Supabase,
  không kéo dữ liệu qua Vercel; API `/api/recordings` cache 60s.

### 9.6 Hằng số dùng chung
`cloud_daemon.py`, `api_server.py`, `gdrive_manager.py`, `auto_h264.py`, `notifier.py`,
`recorder_core.py`… đều lấy `BASE_DIR` / `CONFIG_FILE` từ `constants.py` thay vì tự suy diễn
`os.path.dirname(__file__)`. Nhờ vậy mọi tiến trình chạy từ bất kỳ thư mục nào cũng trỏ đúng thư mục
dự án, và tránh lỗi thiếu khai báo khi khởi chạy trên runner sạch.

---

## 10. Kiểm thử & vận hành cục bộ (tuỳ chọn — chỉ khi phát triển)

> Chạy production **không cần** phần này: ghi hình do GitHub Actions, API do Render, web do Vercel đảm
> nhiệm. Các lệnh dưới đây chỉ dùng để test/sửa mã trên máy cá nhân.

```bash
pip install -r requirements.txt
python -m unittest test_audit_suite.py     # Bộ test hồi quy (CI chạy bộ này)
run_cloud_daemon.bat                        # daemon local + watchdog 5s
run_recorder.bat                            # CLI menu
python api_server.py                        # REST API :8000
python tiktok_recorder.py --user <name> --now
python repair_cloud_videos_engine.py --today
python gdrive_auth.py                        # cấp refresh token OAuth (1 lần)
```

Bộ test dùng `unittest` + `mock` thuần — **mọi lời gọi mạng (TikTok, Google Drive, Supabase) đều bị
mock**, nên chạy được offline. Sau khi chạy còn residue (`.locks/*.recording.lock` và vài thư mục test
rỗng) — xoá trước khi commit.

---

## 11. Tích hợp web-truyen (`D:\web-truyen\kuruRecord`)

`D:\web-truyen` **đã nhúng một bản sao công cụ này** tại `D:\web-truyen\kuruRecord\` và có workflow
`.github/workflows/recorder.yml` chạy với `defaults.run.working-directory: ./kuruRecord`.

> **Không cần local**: API recorder chuẩn là bản cloud `https://tiktok-api-i0o8.onrender.com` (Render
> Free). Cloudflare Tunnel → `localhost:8000` chỉ là **đường tăng tốc tuỳ chọn** khi máy dev đang chạy
> `api_server.py`; web vẫn hoạt động đầy đủ nếu không có local.

```text
Browser /record ──PIN──► Next.js /api/record/[...path] ──► TIKTOK_RECORDER_API_URL
                        (RECORD_PIN, timeout 25s,           ▲ fallback cloud
                         chặn video bytes)                   https://tiktok-api-i0o8.onrender.com
/api/record/dispatch ──GITHUB_PAT──► GitHub Actions recorder.yml ──► kuruRecord/cloud_daemon.py
/api/record/sync ──PIN|CRON_SECRET──► scripts/auto-sync-recordings.mjs ──► Supabase + covers/
app/record/page.js ─────────────────► Supabase (tiktok_*) + /api/record/* + Drive CDN
```

**Thành phần chính**
* `app/record/page.js` — dashboard: PIN gate, CRUD streamer, trạng thái live, lưới video, modal phát
  (HTML5 + iframe Drive), cache localStorage → Supabase → API.
* `app/api/record/[...path]/route.js` — proxy mọi endpoint của tool; tự chọn `TIKTOK_RECORDER_API_URL`
  (Render cloud; có thể ưu tiên Cloudflare Tunnel → localhost nếu muốn tăng tốc lúc dev); xử lý đặc biệt
  `stream-video-id/<id>` (proxy Range 206) và `download-drive/<id>`; **chặn kéo `video/*` qua server** để
  giữ 0-byte bandwidth.
* `app/api/record/dispatch/route.js` — kích hoạt `recorder.yml` trên GitHub Actions.
* `app/api/record/sync/route.js` + `scripts/auto-sync-recordings.mjs` — poll `/api/recordings` (30s),
  tải thumbnail, upsert `covers/record-thumbnails/...` và `tiktok_recordings`.
* `lib/record-queue.js` — tuần tự hoá mọi call API recorder; `lib/supabase.js` — client Supabase.
* `start-all-services.mjs` / `start.bat` — **tuỳ chọn**: chạy local `api_server.py:8000` + sync + cloudflared.

**Env web-truyen cần có**: `TIKTOK_RECORDER_API_URL`, `RECORD_PIN` (mặc định code `'2026'`),
`GITHUB_PAT`/`WORKFLOW_PAT`, `TIKTOK_RECORDER_REPO_OWNER`/`_REPO_NAME`,
`CRON_SECRET`, `NEXT_PUBLIC_SUPABASE_URL`/`_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.

**Nâng cấp bản nhúng `kuruRecord/`** (đang cũ hơn `D:\tiktok-recorder`): copy 13 file `.py` lõi
(`cloud_daemon.py`, `recorder_core.py`, `auto_h264.py`, `staging_queue.py`, `supabase_sync.py`,
`gdrive_manager.py`, `gdrive_auth.py`, `api_server.py`, `repair_cloud_videos_engine.py`, `notifier.py`,
`config_lock.py`, `test_audit_suite.py`, …) — **không** copy `config.json`/`cookies.json`/`.env`; sau đó
chạy lại `test_audit_suite.py` trong `kuruRecord/`.

**Điểm cần khớp khi tích hợp**
1. Schema `tiktok_streamers`/`tiktok_recordings` dùng tên cột `filename`/`username` (khớp code), không
   phải `file_name`/`streamer_name`.
2. Thumbnail đúng path `covers/record-thumbnails/{user}/{base}.jpg` để UI nhận là "sẵn sàng".
3. Không kéo `video/*` qua proxy Vercel/Render — luôn phát qua `stream-video-id` hoặc iframe Drive.
4. Đồng bộ phiên bản: `kuruRecord/` cũ hơn bản nguồn (thiếu `config_lock`/VIP/DRAIN/heartbeat mới).
5. Workflow đôi: nâng cấp thì cả 2 repo đều cần sync workflow.

---

## 12. Bảo mật & nguyên lý thiết kế quan trọng

### Bảo mật
* Supabase anon key **không** hardcode trong source — đọc env → `config.json:supabase_key`.
* **PIN auth opt-in**: middleware chặn POST/PUT/PATCH/DELETE khi có `RECORD_PIN`/`api_pin`; GET công khai.
* `is_recorder_owned_file()` chặn ép public file lạ — `/api/stream-video-id/*` trả **403** nếu `file_id`
  không thuộc `tiktok-record/`; path traversal bị chặn bằng `Path.is_relative_to(BASE_DIR)`.
* CORS `*` + GET không auth → chỉ mở trong nội bộ hoặc đặt sau proxy (cách web-truyen đang làm).
* `config.json` chứa secret thật (đã `.gitignore`) — **không commit**; nên rotate token nếu repo từng public.

### Nguyên lý thiết kế
* **HLS**: bỏ `-reconnect_at_eof`/`-reconnect_streamed` cho `.m3u8` (chỉ giữ cho `.flv`) — tránh đóng
  băng 0.00 MB.
* **Cookie**: luôn khởi tạo `cookies = {}` trước khi nạp — tránh `UnboundLocalError` trên Render.
* **Đa độ phân giải**: tự tách part khi độ phân giải đổi giữa chừng — tránh khung xám/nhiễu macroblock.
* **Đồng bộ web**: đẩy `is_live`/`is_recording`/`status` lên Supabase theo thời gian thực.
* **TikTok 18+/VIP**: nạp cookie trước mọi call + nhiều nhánh fallback.

### Chưa xử lý dứt điểm
* Worker vẫn có thể tích lũy segment từ nhiều tier trong cùng một part — `concat_mp4_segments` mới chỉ
  **cảnh báo** (probe 3 segment), chưa chốt part khi đổi độ phân giải. Dứt điểm thì tách part ngay khi
  độ phân giải đổi.
* `+nobuffer` có thể bắt đầu ghi **giữa GOP** (1–N frame lỗi) — cần chờ keyframe đầu.

---

*Tài liệu mô tả hệ thống tại `D:\tiktok-recorder` và tích hợp `D:\web-truyen`.*
