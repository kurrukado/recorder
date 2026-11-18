import os


def capture_mode():
    return os.getenv("RECORD_WORKER_MODE", "").strip().lower()


def canary_users():
    return {name.strip().lstrip("@").lower() for name in os.getenv("RECORD_WORKER_CANARY", "").split(",") if name.strip()}


def worker_owns_capture(username):
    mode = capture_mode()
    return mode == "active" or (mode == "canary" and username in canary_users())
