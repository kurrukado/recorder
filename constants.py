"""
Constants used throughout the TikTok Recorder system.
Centralized to avoid duplication and ensure consistency.
"""

import os

# File paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.json")
LOCK_FILE = os.path.join(BASE_DIR, "config.lock")
PROGRESS_FILE = os.path.join(BASE_DIR, "cloud_repair_progress.json")
SCAN_REPORT_PATH = os.path.join(BASE_DIR, "scan_report.json")

# Recording defaults
DEFAULT_CONFIG = {
    "target_user": "islizanx",
    "check_interval_seconds": 20,
    "quality": "best",
    "output_dir": ".",
    "auto_upscale_1080p": True,
    "max_recording_seconds": 3600,
    "offline_confirm_seconds": 300,
    "timezone_offset_hours": 7,
    "google_client_id": "",
    "google_client_secret": "",
    "gdrive_enabled": True,
    "gdrive_refresh_token": "",
    "supabase_key": "",
    "api_pin": "",
    "monitored_users": []
}

# FFmpeg settings
FFMPEG_PRESETS = {
    "best": "-c:v libx264 -crf 18 -preset veryfast -c:a aac -b:a 128k",
    "good": "-c:v libx264 -crf 23 -preset fast -c:a aac -b:a 128k",
    "fast": "-c:v libx264 -crf 28 -preset ultrafast -c:a aac -b:a 64k"
}

# Resolution handling
SUPPORTED_RESOLUTIONS = {
    "1080p": {"width": 1920, "height": 1080},
    "720p": {"width": 1280, "height": 720},
    "480p": {"width": 854, "height": 480},
    "360p": {"width": 640, "height": 360}
}

# Cloud settings
GDRIVE_FOLDER_IDS = {
    "root": "tiktok-record",
    "staging": "_staging"
}

# Time constants
TARGET_QUEUE_SECONDS = 3600  # 1 hour for staging queue
MAX_IDLE_SECONDS = 300  # 5 minutes offline before flushing queue
LOCK_TIMEOUT_SECONDS = 10.0  # Config lock timeout

# Video processing
MIN_VIDEO_DURATION = 300  # 5 minutes minimum for standalone upload
MAX_STAGING_SEGMENTS = 50  # Max segments per staging queue

# API endpoints
SUPABASE_URL = "https://jetwtqakyxjcffbwhhot.supabase.co"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
GOOGLE_DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"

# User agent for API requests
DEFAULT_USER_AGENT = "TikTokRecorderBot/1.0"