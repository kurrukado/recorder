#!/usr/bin/env python3
"""
Quick API test for TikTok Recorder
"""

import requests
import time
import subprocess
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8002
API_URL = f"http://localhost:{PORT}"

def test_health():
    """Test /api/health endpoint"""
    try:
        resp = requests.get(f"{API_URL}/api/health", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Health check OK: {data}")
            return True
        else:
            print(f"❌ Health check failed: {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Health check error: {e}")
        return False

def test_streamers():
    """Test /api/streamers endpoint"""
    try:
        resp = requests.get(f"{API_URL}/api/streamers", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            streamers = data.get("streamers", [])
            print(f"✅ Streamers: {len(streamers)} found")
            return True
        else:
            print(f"❌ Streamers endpoint: {resp.status_code} - {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"❌ Streamers error: {e}")
        return False

def main():
    print("🧪 Quick API Test")
    
    # Check if server is running
    try:
        if test_health():
            print("\n✅ API Server đang chạy")
            test_streamers()
        else:
            print("\n⚠️ API Server không chạy, thử khởi động...")
            
            # Try to start server
            cmd = [sys.executable, "-m", "uvicorn", "api_server:app", 
                   "--host", "0.0.0.0", "--port", str(PORT), "--log-level", "error"]
            
            proc = subprocess.Popen(cmd, cwd=BASE_DIR)
            print(f"  Server started (PID: {proc.pid})")
            time.sleep(3)
            
            if test_health():
                print("✅ Server started successfully")
                test_streamers()
                proc.terminate()
            else:
                print("❌ Server không khởi động được")
                proc.terminate()
                return False
                
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)