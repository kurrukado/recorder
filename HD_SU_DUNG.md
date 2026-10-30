# 📖 HƯỚNG DẪN SỬ DỤNG HỆ THỐNG TIKTOK RECORDER 24/7 & CLOUD API (v67)

Hệ thống tự động theo dõi và ghi hình livestream TikTok chuẩn HD H.264 (AVC), tự động đóng gói qua **Staging Queue**, đồng bộ dữ liệu vào **Supabase** và lưu trữ video vĩnh viễn trên **Google Drive 5TB**.

> [!IMPORTANT]
> **HỆ THỐNG HOẠT ĐỘNG 100% TỰ ĐỘNG TRÊN ĐÁM MÂY — KỂ CẢ KHI TẮT MÁY TÍNH CÁ NHÂN!**
> Bạn **KHÔNG CẦN** bật máy tính cá nhân. Toàn bộ tiến trình theo dõi, ghi hình, chuyển đổi video, upload Google Drive và Web API đều chạy 24/7 độc lập trên nền tảng **GitHub Actions Cloud** (hoàn toàn miễn phí, không giới hạn số phút) kết hợp cùng **Supabase Cloud** và **Vercel Edge**.

---

## 📑 MỤC LỤC
1. [Kiến trúc Hệ thống Đám Mây 24/7 (Không Cần Bật Máy Tính)](#1-kiến-trúc-hệ-thống-đám-mây-247-không-cần-bật-máy-tính)
2. [Các Tính Năng Nổi Bật](#2-các-tính-năng-nổi-bật)
3. [Cấu Trúc Lưu Trữ (Google Drive & Supabase)](#3-cấu-trúc-lưu-trữ-google-drive--supabase)
4. [Vận hành 24/7 trên GitHub Actions (Cloud Runner)](#4-vận-hành-247-trên-github-actions-cloud-runner)
5. [Môi trường Kiểm thử Cục bộ (Tùy chọn dành cho Lập trình / Debug)](#5-môi-trường-kiểm-thử-cục-bộ-tùy-chọn-dành-cho-lập-trình--debug)
6. [Tích hợp Web API & Chế độ Chuyển mạch Đám mây Serverless 24/7](#6-tích-hợp-web-api--chế-độ-chuyển-mạch-đám-mây-serverless-247)
7. [Xem Video Trực Tuyến & Tải Tốc Độ Cao (Google Edge CDN)](#7-xem-video-trực-tuyến--tải-tốc-độ-cao-google-edge-cdn)
8. [Giải thích Chi tiết Các File trong Mã Nguồn](#8-giải-thích-chi-tiết-các-file-trong-mã-nguồn)
9. [Cập nhật Cookie TikTok Khi Cần](#9-cập-nhật-cookie-tiktok-khi-cần)

---

## 1. KIẾN TRÚC HỆ THỐNG 24/7 KHÔNG GIỚI HẠN QUOTA

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        WEBSITE CỦA BẠN                                 │
│   (Quản lý Streamer, Xem Video HTML5 trực tiếp, Tải CDN HTTP Range 206)│
└───────────────────▲──────────────────────────────▲─────────────────────┘
                    │                              │
         Supabase PostgREST API            Google Edge CDN URL
       (Thêm/Xóa Streamer, Đọc Video)    (Phát video tua mượt mà)
                    │                              │
                    ▼                              │
┌──────────────────────────────────────┐           │
│           SUPABASE CLOUD             │           │
│  - Table: tiktok_streamers (Danh sách)│           │
│  - Table: tiktok_recordings (Video)  │           │
│  - Storage Bucket: covers/           │           │
│    (Ảnh Thumbnail sắc nét CDN)       │           │
└───────────────────▲──────────────────┘           │
                    │                              │
      Đồng bộ trạng thái & metadata                │
                    │                              │
┌───────────────────┴──────────────────┐           │
│    GITHUB ACTIONS (PUBLIC RUNNER)    │           │
│  - Chạy 24/7 vĩnh viễn (UNLIMITED)   │           │
│  - Quét streamers từ Supabase mỗi 15s│           │
│  - Fast-Poll 5s bắt streamer mới     │           │
│  - Ghi đồng thời lên đến 10 streamer │           │
│  - Continuous Relay + Cron 50 phút   │           │
│  - Ưu tiên HLS H.264 chống lỗi NAL   │           │
│  - Watchdog tự lành (6 giờ/lần)      │           │
└───────────────────┬──────────────────┘           │
                    │                              │
                    │ Tải video MP4 & Thumbnail    │
                    ▼                              │
┌──────────────────────────────────────────────────┴─────────────────────┐
│                             GOOGLE DRIVE                               │
│  - Thư mục gốc: tiktok-record/<tên_streamer>/                           │
│  - Staging Queue (phân đoạn ngắn chống mất video khi rớt mạng/hết giờ) │
│  - Video chính thức MP4 (+faststart) & Thumbnail JPG                   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. CÁC TÍNH NĂNG NỔI BẬT

* ☁️ **Hoạt Động 100% Đám Mây 24/7 (Không Phụ Thuộc Máy Tính Cá Nhân):**
  - Quét danh sách streamer mỗi 15 giây (Supabase là nguồn chuẩn) + Fast-Poll 5 giây riêng cho streamer vừa thêm.
  - Người dùng có thể tắt máy tính hoàn toàn, hệ thống vẫn duy trì ghi hình, upload Google Drive và cập nhật Supabase 24/7.
* ⚡ **Thêm Streamer Lúc Đang Live → Ghi Hình Trong ~5–8 Giây:**
  - Khi bạn thêm một streamer đang phát live, web hiện `🔴 LIVE` ngay lập tức (do API kiểm tra TikTok trực tiếp), sau đó daemon bật `is_recording = true` và hiện `🔴 REC` sau **~5–8 giây** nếu runner đang chạy.
  - Cơ chế: Fast-Poll 5s phát hiện username mới → thoát sleep sớm → kiểm tra live **đầu tiên** trong hàng đợi → khởi động luồng ghi.
  - Nếu **không có runner nào đang chạy**, cần chờ runner khởi động (~1–3 phút: boot + cài ffmpeg/deps). Nếu web không cấu hình `GITHUB_PAT`/`WORKFLOW_PAT` thì phải chờ cron 50 phút.
* ♾️ **100% Unlimited Minutes (Public Repo):**
  - Chạy trên GitHub Actions ở chế độ **Public Repository** được hưởng chính sách **Không giới hạn phút chạy miễn phí** của GitHub (giải quyết triệt để hạn mức 2.000 phút/tháng của repo Private).
  - Toàn bộ thông tin nhạy cảm (`GDRIVE_REFRESH_TOKEN`, `SUPABASE_KEY`...) được bảo vệ tuyệt đối qua **GitHub Secrets**, `.gitignore` ngăn chặn 100% rò rỉ mã bí mật.
* 🛡️ **Khắc Phục Triệt Để Lỗi Bitstream NAL Unit (FLV Sequence Header):**
  - Cơ chế nhận diện luồng thông minh: Luôn ưu tiên luồng chuẩn **HLS H.264 (MPEG-TS)** thay vì FLV.
  - Tích hợp bộ tiền xử lý **ISO/IEC 14496-15 AVCC Bitstream Sanitizer** trong `auto_h264.py`: Tự động loại bỏ mã rác `01 64 00 1f...` do TikTok nhúng vào đầu keyframe, giải quyết dứt điểm lỗi màn hình đen hoặc thông báo *"Video file is processing slowly due to exceeding upload quota limits"* trên Google Drive.
* 📦 **Ghi Theo Part 1 Giờ + Graceful Drain (Chống Mất Video Khi Luân Chuyển Runner):**
  - Video được ghi thành từng part tối đa 1 giờ (`MAX_CHUNK_SECONDS = 3600`), **mỗi part upload lên Drive & Supabase ngay khi hoàn tất** (không dồn tới cuối buổi live).
  - Khi runner hết phiên (170 phút), daemon vào pha **DRAIN**: ngừng nhận streamer mới, phát lệnh dừng mềm để ffmpeg chốt file, chờ tối đa 6 phút cho các luồng upload xong rồi mới kết thúc job.
  - ⚠️ **Giới hạn thật:** nếu runner bị **cancel / kill cứng** (hủy job, runner bị thu hồi), part đang ghi dở (tối đa 1 giờ) **sẽ mất** vì đĩa của runner là tạm và file chưa được upload. Module `staging_queue.py` (upload từng đoạn ngắn lên Drive) có sẵn nhưng **chưa được nối vào luồng ghi chính** — hiện chỉ dùng cho hàm dọn `check_and_flush_idle_queues()`.
* 🗑️ **Dọn Sạch 100% Cover Thumbnail Supabase Storage Khi Xóa:**
  - Khi xóa streamer: Tự động quét và dọn sạch toàn bộ thư mục ảnh thumbnail `record-thumbnails/{user}/` trong bucket `covers` trên Supabase Storage, đồng thời xóa bản ghi trong database.
  - Khi xóa video lẻ hoặc hàng loạt: Tự động xóa chính xác file ảnh `.jpg` tương ứng trong bucket `covers`, không để lại rác bộ nhớ.
* ⚡ **Serverless Cloud Failover 24/7 Trên Web:**
  - Web API trên Vercel tự động nhận diện và chuyển mạch sang Supabase Cloud trong 0.05 giây nếu máy tính cá nhân tắt, giữ trạng thái trực tuyến (Online) 24/7 liên tục.
* 🩺 **Watchdog Tự Lành (Self-Healing Engine):**
  - Workflow `.github/workflows/repair-watchdog.yml` chạy tự động mỗi 6 giờ (`0 */6 * * *`) kích hoạt `repair_cloud_videos_engine.py --today`.
  - Tự động rà soát mọi bản ghi trong ngày, sửa chữa bitstream in-place trên Google Drive và bù thumbnail vào Supabase Storage nếu có phiên bị lỗi mạng.
* ⚡ **Live-End Watchdog & Tách File 1 Tiếng:**
  - Tự động nhận biết phòng live tắt sóng trong ~7 giây (kiểm tra lại 2 lần), sau đó chờ `offline_confirm_seconds` (mặc định 300s) để chắc chắn không phải đứt CDN thoáng qua rồi mới chốt video.
  - Hỗ trợ tách file 1 giờ (`MAX_CHUNK_SECONDS = 3600`) cho các phiên phát trực tiếp xuyên đêm, tránh tràn bộ nhớ đệm.
* 🌐 **Google Edge CDN Tua Video Tức Thì (HTTP Range 206):**
  - Tự động gán cờ `+faststart` (đưa atom `moov` lên đầu file) cho phép trình duyệt HTML5 phát ngay lập tức và tua mượt mà đến mọi giây của video.

---

## 3. CẤU TRÚC LƯU TRỮ (GOOGLE DRIVE & SUPABASE)

### 1. Trên Google Drive
```text
Google Drive
└── tiktok-record/
    ├── active_recordings.json          <- Trạng thái phiên ghi hình đang hoạt động (TTL 180s)
    ├── streamers.json                  <- Bản sao danh sách streamer (fallback khi Supabase lỗi)
    ├── islizanx/
    │   ├── islizanx_2026-09-27_10-00-00.mp4
    │   ├── islizanx_2026-09-27_10-00-00.jpg
    │   ├── islizanx_2026-09-27_11-00-00_part2.mp4   <- Part 2 khi live > 1 giờ
    │   └── _staging/                   <- Chỉ tồn tại nếu module staging_queue.py được dùng
    └── triuthnga770/
        ├── triuthnga770_2026-09-27_15-02-14.mp4
        └── triuthnga770_2026-09-27_15-02-14.jpg
```

### 2. Trên Supabase Cloud
* **Table `tiktok_streamers`:** Chứa danh sách các streamer được theo dõi (`username`, `created_at`).
* **Table `tiktok_recordings`:** Lưu trữ lịch sử toàn bộ video (`id`, `username`, `filename`, `size_mb`, `drive_file_id`, `thumbnail_url`, `created_at`).
* **Storage Bucket `covers`:** Lưu ảnh thumbnail chất lượng cao tại đường dẫn `record-thumbnails/{user}/{filename}.jpg`.

---

## 4. VẬN HÀNH 24/7 TRÊN GITHUB ACTIONS (CLOUD RUNNER)

### Bước 1: Thiết lập Quyền Workflow
1. Mở repository: `https://github.com/kurrukado/recorder`
2. Vào **Settings** ➔ Menu bên trái chọn **Actions** ➔ **General**.
3. Kéo xuống mục **Workflow permissions** ➔ Chọn: **Read and write permissions** ➔ Bấm **Save**.

### Bước 2: Cài đặt 5 Biến Secrets Bắt Buộc
Vào **Settings** ➔ **Secrets and variables** ➔ **Actions** ➔ Bấm **New repository secret**:

| Tên Secret | Giá trị |
| :--- | :--- |
| `GOOGLE_CLIENT_ID` | Client ID ứng dụng Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Client Secret ứng dụng Google Cloud |
| `GDRIVE_REFRESH_TOKEN` | OAuth2 Refresh Token Google Drive |
| `SUPABASE_URL` | URL dự án Supabase (VD: `https://jetwtqakyxjcffbwhhot.supabase.co`) |
| `SUPABASE_KEY` | Supabase Anon Key hoặc Service Role Key |
| `WORKFLOW_PAT` | *(Tùy chọn)* GitHub Personal Access Token để kích hoạt relay ngay lập tức |
| `TIKTOK_SESSION_ID` | *(Tùy chọn)* Cookie `sessionid_ss` nếu cần ghi live 18+/VIP |

### Bước 3: Kích hoạt Runner
1. Vào tab **Actions** trên GitHub.
2. Chọn workflow **TikTok 24-7 Auto Recorder** ở cột bên trái.
3. Bấm **Run workflow** ➔ **Run workflow**.
*(Sau khi kích hoạt, runner sẽ tự động relay nối tiếp nhau và duy trì 24/7 bằng cron fallback 50 phút).*

---

## 5. MÔI TRƯỜNG KIỂM THỬ CỤC BỘ (TÙY CHỌN DÀNH CHO LẬP TRÌNH / DEBUG)

> [!NOTE]
> **HỆ THỐNG KHÔNG YÊU CẦU BẬT MÁY TÍNH CÁ NHÂN ĐỂ HOẠT ĐỘNG!**
> Việc chạy cục bộ chỉ là **tùy chọn phụ** phục vụ mục đích kiểm thử code khi lập trình (dev/debug) hoặc khi bạn chủ động muốn dùng thêm GPU rời NVIDIA (NVENC).
> Trong chế độ hoạt động bình thường, toàn bộ tiến trình ghi hình tự động chạy trên **GitHub Actions Cloud** và Web API chạy trên **Vercel Edge + Supabase Cloud**. Bạn hoàn toàn có thể tắt máy tính cá nhân đi ngủ.

Nếu cần chạy thử nghiệm hoặc gỡ lỗi cục bộ trên máy tính cá nhân:
1. Cài đặt thư viện Python:
   ```cmd
   pip install -r requirements.txt
   ```
2. Kiểm tra cú pháp toàn bộ module (không cần credentials):
   ```cmd
   python -m py_compile cloud_daemon.py recorder_core.py supabase_sync.py api_server.py auto_h264.py
   ```
3. Kiểm tra hệ thống đám mây có đang chạy thật không (đọc trực tiếp Supabase):
   ```cmd
   python check_status.py
   ```
   → In ra tổng streamer, số đang live/ghi hình, tuổi heartbeat của từng luồng và cảnh báo ghost recording.
4. Chạy daemon ghi hình thử nghiệm với GPU:
   ```cmd
   run_cloud_daemon.bat
   ```

> [!WARNING]
> Repo **không còn test suite tự động**. Các file `test_audit_suite.py`, `test_challenger_concurrency.py`, `test_adversarial_deletion.py` và workflow `ci-tests.yml` đã bị xóa ở commit `52418b8`. Mọi con số "78/78" hay "104/104 test" trong tài liệu cũ là số liệu lịch sử và **không thể tái lập**. Dùng `py_compile` + `check_status.py` để kiểm tra sau khi sửa code.

---

## 6. TÍCH HỢP WEB API & CHẾ ĐỘ CHUYỂN MẠCH ĐÁM MÂY SERVERLESS 24/7

Website Kuru Hub (`web-truyen`) được thiết kế theo kiến trúc **Cloud-First & Zero-Downtime**:

### 1. Cơ chế Tự Động Chuyển Mạch Đám Mây (Serverless Cloud Failover)
* **Khi máy tính cá nhân BẬT:** Web API tự động định tuyến về máy tính cá nhân (qua Cloudflare Tunnel) để tối ưu độ trễ.
* **Khi máy tính cá nhân TẮT:** Web API trên Vercel tự động nhận diện và chuyển mạch sang **Supabase Cloud trong 0.05 giây**:
  - Không gây timeout hay lỗi `502 Ngoại tuyến`.
  - Hiển thị trực tuyến 24/7 (`mode: cloud_supabase_247`).
  - Cho phép xem streamer, kiểm tra live status, thêm/xóa streamer bình thường.

### 2. Quản lý Streamer & Xóa Dữ Liệu Sạch Sẽ (Cả DB lẫn Storage Covers)
* **Thêm streamer mới:** Lưu vào Supabase bảng `tiktok_streamers`. Runner đám mây phát hiện qua **Fast-Poll 5 giây** và kiểm tra live **đầu tiên** trong hàng đợi → bật ghi hình trong **~5–8 giây** nếu runner đang chạy.
* **Xóa streamer:** Xóa khỏi `tiktok_streamers`, `tiktok_recordings`, đồng thời **xóa sạch toàn bộ ảnh thumbnail** trong bucket `covers` trên Supabase Storage (`record-thumbnails/{user}/`).
* **Xóa video đã ghi:** Xóa hàng tương ứng trong `tiktok_recordings`, xóa video trên Google Drive và **xóa sạch file ảnh thumbnail `.jpg`** tương ứng trong bucket `covers`.

### 2b. Bảng Độ Trễ Thực Tế Khi Thêm Streamer Đang Live

| Tình huống | Thời gian tới khi có `is_recording = true` |
| :--- | :--- |
| Runner đang chạy + còn slot (< 10) | **~5–8 giây** (Fast-Poll 5s + kiểm tra live + khởi động luồng) |
| Runner đang chạy nhưng streamer vừa fail phiên trước | **~5–8 giây** (backoff của username mới được reset tự động) |
| Runner đang ở pha DRAIN (sau 170 phút) | ~5–10 phút (chờ runner kế tiếp khởi động) |
| Không có runner + Vercel có `GITHUB_PAT`/`WORKFLOW_PAT` | ~1–3 phút (boot runner + cài ffmpeg/deps) |
| Không có runner + **không** có PAT | tới **50 phút** (chờ cron `*/50 * * * *`) |
| Đã đủ 10 streamer đang ghi | Không ghi; chỉ cập nhật `status='live'` để web hiển thị 🟢 LIVE |

> [!NOTE]
> Mỗi lần thêm streamer, web gọi Dispatch API. Do workflow dùng `concurrency: recorder-runner` với `cancel-in-progress: false`, nếu đang có run thì run mới sẽ **xếp hàng** (không hủy run đang ghi) — đây là hành vi cố ý để không làm mất video đang ghi.

### 3. Kích hoạt Ghi hình Tức thì qua GitHub Actions Dispatch API
Gửi request từ Next.js / Node.js backend khi cần ép runner khởi chạy ngay lập tức:
```javascript
await fetch('https://api.github.com/repos/kurrukado/recorder/actions/workflows/recorder.yml/dispatches', {
  method: 'POST',
  headers: {
    'Accept': 'application/vnd.github+v3+json',
    'Authorization': `Bearer ${GITHUB_PAT}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ ref: 'main' })
});
```

---

## 7. XEM VIDEO TRỰC TUYẾN & TẢI TỐC ĐỘ CAO (GOOGLE EDGE CDN)

Mỗi bản ghi trả về mã `drive_file_id`. URL phát video chuẩn Google Edge CDN:
`https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t`

### Nhúng phát trực tiếp HTML5:
```html
<video width="720" height="1280" controls poster="THUMBNAIL_URL_SUPABASE">
  <source src="https://drive.usercontent.google.com/download?id=DRIVE_FILE_ID&export=download&authuser=0&confirm=t" type="video/mp4">
  Trình duyệt của bạn không hỗ trợ phát video HTML5.
</video>
```

---

## 8. GIẢI THÍCH CHI TIẾT CÁC FILE TRONG MÃ NGUỒN

| Tên file | Chức năng chính |
| :--- | :--- |
| `cloud_daemon.py` | Tiến trình điều phối chính, đồng bộ streamer từ Supabase (15s + Fast-Poll 5s), quản lý đa luồng tối đa 10 streamer |
| `recorder_core.py` | Lõi trích xuất luồng TikTok (ưu tiên HLS H.264), Live-End Watchdog, FFmpeg recorder |
| `auto_h264.py` | Bộ xử lý bitstream ISO/IEC 14496-15, sửa lỗi NAL Unit, trích xuất thumbnail chính giữa |
| `staging_queue.py` | Module đệm phân đoạn lên Drive — **hiện chưa nối vào luồng ghi chính**, chỉ dùng cho `check_and_flush_idle_queues()` |
| `supabase_sync.py` | Khách REST client đồng bộ dữ liệu hai chiều với Supabase Database & Storage |
| `repair_cloud_videos_engine.py` | Động cơ quét và tự lành video trên Google Drive, bù thumbnail thiếu |
| `gdrive_manager.py` | Quản lý Google Drive API v3: Upload đa phân đoạn, phân quyền CDN, dọn rác |
| `gdrive_auth.py` | Bộ cấp phép và tự động làm mới OAuth2 Refresh Token Google Drive |
| `api_server.py` | Máy chủ REST API FastAPI cung cấp các endpoint bổ trợ và kiểm thử |
| `check_status.py` | Kiểm tra nhanh trạng thái thật từ Supabase: live/recording, tuổi heartbeat, ghost recording |
| `cleanup_ghost_recordings.py` | Dọn cờ `is_recording`/`is_live` sai lệch so với thực tế TikTok |
| `run_cloud_daemon.bat` | Script chạy ngầm cục bộ với GPU NVIDIA NVENC, tự khởi động lại sau 5s |
| `.github/workflows/recorder.yml` | Workflow GitHub Actions chạy liên tục 24/7 (Relay + Cron 50 phút) |
| `.github/workflows/repair-watchdog.yml` | Workflow GitHub Actions tự phục hồi chạy định kỳ mỗi 6 giờ |

> [!NOTE]
> Repo không còn test suite tự động (`test_*.py` + `ci-tests.yml` đã xóa ở commit `52418b8`). Kiểm tra sau khi sửa code bằng `python -m py_compile ...` và `python check_status.py`.

---

## 9. CẬP NHẬT COOKIE TIKTOK KHI CẦN

Khi cần ghi hình các phòng live giới hạn độ tuổi (18+) hoặc VIP Sub-Only:
1. Đăng nhập tài khoản TikTok trên trình duyệt máy tính.
2. Bấm phím **F12** ➔ Chọn tab **Application** (hoặc **Bộ nhớ**) ➔ Chọn mục **Cookies** (`https://www.tiktok.com`).
3. Tìm dòng có tên **`sessionid_ss`** và sao chép chuỗi ký tự giá trị.
4. Cập nhật vào secret **`TIKTOK_SESSION_ID`** trong **GitHub Secrets** của repository `kurrukado/recorder`.
