# Recorder - 24/7 Auto Stream Recorder

> [!IMPORTANT]
> This was completely made by AI so it will cause unexpected bugs but i will commit fixes if its happen, it may takes times

---

## 🚀 Key Features
* Streamer monitoring, live detection (< 20s), H.264 stream capture, Google Drive storage, Supabase sync, and web dashboard management
* ☁️ **100% Cloud-Native 24/7 Operation:** Runs continuously via GitHub Actions Continuous Relay and fallback cron (`*/50 * * * *`). Supports recording up to **10 concurrent streamers** for free (unlimited runner minutes on public repositories).
* ⚡ **Instant Web Failover & Realtime Sync:** Web API automatically switches to Supabase Cloud in < 50ms when local instances are offline. Realtime WebSocket events push live/recording status changes to the frontend in < 100ms.
* 🗑️ **Full Storage Cleanup:** Deleting a streamer purges all records and thumbnails in `covers/record-thumbnails/{user}/` on Supabase Storage. Deleting a video removes both the Google Drive file and its Supabase thumbnail.
* 📦 **Zero-Loss Staging Queue:** Automatically splits active streams into safe segments and stores them in Google Drive staging queues to prevent data loss on sudden disconnects or runner rotation. Merges into final MP4 when the live ends.
* 🛡️ **H.264 Normalization & Bitstream Repair:** Prioritizes HLS H.264 MPEG-TS streams to eliminate NAL unit / FLV header errors. Automatically flags `+faststart` (moov atom at file start) for instant browser playback.
* 🎙️ **Audio-Only / Voice Live Support:** Automatically synthesizes a lightweight static H.264 video frame (1 fps) for voice/radio streams so the audio is preserved and playable on Google Drive and HTML5 video players.
* 🩺 **Self-Healing Watchdog:** Recurring watchdog workflow (`0 */6 * * *`) repairs bitstreams and restores missing thumbnails in Supabase Storage.
* 🌐 **Full Supabase & Web API Integration:** Live list synchronization via `tiktok_streamers` and recording metadata/CDN links via `tiktok_recordings`.

---

## ⚙️ GitHub Secrets Setup (Required)

In your GitHub repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret Name | Description | Required |
| :--- | :--- | :--- |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth2 Client ID (Drive API) | Yes |
| `GOOGLE_CLIENT_SECRET` | Google Cloud OAuth2 Client Secret | Yes |
| `GDRIVE_REFRESH_TOKEN` | Google Drive OAuth2 Refresh Token | Yes |
| `SUPABASE_URL` | Supabase Project URL (`https://xxxx.supabase.co`) | Yes |
| `SUPABASE_KEY` | Supabase Anon Key or Service Role Key | Yes |
| `WORKFLOW_PAT` | *(Optional)* GitHub Personal Access Token (`repo`/`workflow`) for instant relay | Optional |
| `TIKTOK_SESSION_ID` | *(Optional)* TikTok `sessionid_ss` cookie for 18+ age-restricted streams | Optional |
| `TELEGRAM_BOT_TOKEN` | *(Optional)* Telegram bot token for instant live alerts | Optional |
| `TELEGRAM_CHAT_ID` | *(Optional)* Telegram chat ID to receive notifications | Optional |

---

## 🌐 Web & API Integration

### 1. Streamer Management (Direct via Supabase)
Add or remove monitored streamers directly through Supabase PostgREST API or Supabase client without an intermediary server:
* **Add Streamer:** `POST /rest/v1/tiktok_streamers` with `{"username": "streamer_name"}`.
* **Remove Streamer:** `DELETE /rest/v1/tiktok_streamers?username=eq.streamer_name`.
* *Cloud runners reload the monitored list from Supabase every 15–20 seconds.*

### 2. Manual Runner Dispatch
Trigger an immediate recording run via GitHub Actions Dispatch API:
```bash
POST https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/recorder.yml/dispatches
Authorization: Bearer <GITHUB_PAT>
Content-Type: application/json

{"ref": "main"}
```

### 3. Display Recordings on Web
Fetch video entries directly from the `tiktok_recordings` table:
* `thumbnail_url`: Public thumbnail image on Supabase Storage.
* `drive_file_id`: Google Drive file ID for HTML5 video playback and direct CDN download.

---

## 🧪 Local Testing & Debugging (Optional)

```bash
# Install dependencies
pip install -r requirements.txt

# Run comprehensive test suite (78 tests)
python -B -m unittest test_audit_suite.py

# (Optional) Run recording daemon locally with GPU NVENC
run_cloud_daemon.bat
```