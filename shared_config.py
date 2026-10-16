"""
Unified configuration management for TikTok Recorder.
Provides thread-safe and process-safe config loading/saving.
"""

import os
import json
from typing import Dict, Any, Optional
from config_lock import config_transaction
import constants

def load_config() -> Dict[str, Any]:
    """
    Load configuration from config.json with locking.
    
    Returns:
        Dict containing all configuration values.
    """
    with config_transaction():
        if not os.path.exists(constants.CONFIG_FILE):
            return constants.DEFAULT_CONFIG.copy()
        
        try:
            with open(constants.CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"[!] Lỗi khi đọc config.json: {e}")
            return constants.DEFAULT_CONFIG.copy()
        
        # Merge with defaults to ensure all keys exist
        merged = constants.DEFAULT_CONFIG.copy()
        merged.update(config)
        return merged

def save_config_fields(fields: Dict[str, Any]) -> bool:
    """
    Save specific fields to config.json with atomic write.
    
    Args:
        fields: Dictionary of field-value pairs to update
        
    Returns:
        True if successful, False otherwise
    """
    with config_transaction():
        # Load existing config
        config = load_config()
        
        # Update with new fields
        config.update(fields)
        
        # Atomic write using temporary file
        tmp_path = constants.CONFIG_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            
            # Atomic replace
            os.replace(tmp_path, constants.CONFIG_FILE)
            return True
            
        except Exception as e:
            print(f"[!] Không ghi được config.json: {e}")
            # Clean up temp file
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False

def save_config(config: Dict[str, Any]) -> bool:
    """
    Save entire config dictionary to config.json.
    
    Args:
        config: Complete configuration dictionary
        
    Returns:
        True if successful, False otherwise
    """
    with config_transaction():
        # Atomic write using temporary file
        tmp_path = constants.CONFIG_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            
            # Atomic replace
            os.replace(tmp_path, constants.CONFIG_FILE)
            return True
            
        except Exception as e:
            print(f"[!] Không ghi được config.json: {e}")
            # Clean up temp file
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False

def get_config_value(key: str, default: Any = None) -> Any:
    """
    Get a single configuration value.
    
    Args:
        key: Configuration key to retrieve
        default: Default value if key doesn't exist
        
    Returns:
        Configuration value or default
    """
    config = load_config()
    return config.get(key, default)

def set_config_value(key: str, value: Any) -> bool:
    """
    Set a single configuration value.
    
    Args:
        key: Configuration key to set
        value: Value to set
        
    Returns:
        True if successful, False otherwise
    """
    return save_config_fields({key: value})

def reload_config() -> Dict[str, Any]:
    """
    Force reload configuration from disk (bypass cache).
    
    Returns:
        Dict containing all configuration values.
    """
    # Clear any potential in-memory cache by loading fresh
    return load_config()

# Backward compatibility aliases
# These allow existing code to continue working without changes
def load_cookies() -> Dict[str, Any]:
    """
    Load TikTok cookies from multiple possible locations.
    
    Checks:
    1. cookies.json in current directory
    2. web-truyen/kuruRecord/cookies.json (compatibility)
    3. Environment variables
    4. Config file
    
    Returns:
        Dict containing cookie data.
    """
    cookies = {}
    cookie_paths = [
        constants.COOKIES_FILE,
        os.path.join(os.path.dirname(constants.BASE_DIR), "web-truyen", "kuruRecord", "cookies.json")
    ]
    
    for cp in cookie_paths:
        if os.path.exists(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if v:
                            cookies[k] = str(v).strip()
                    if cookies:
                        break
            except Exception:
                pass

    if not cookies:
        env_session = os.environ.get("TIKTOK_SESSION_ID", "").strip()
        if env_session:
            cookies = {"sessionid_ss": env_session, "sessionid": env_session}

    if not cookies:
        try:
            cfg = load_config()
            cfg_session = (cfg.get("tiktok_session_id") or cfg.get("sessionid_ss") or "").strip()
            if cfg_session:
                cookies = {"sessionid_ss": cfg_session, "sessionid": cfg_session}
        except Exception:
            pass

    if not cookies:
        env_sessionid_ss = os.environ.get("TIKTOK_SESSIONID_SS", "").strip()
        if env_sessionid_ss:
            cookies = {"sessionid_ss": env_sessionid_ss, "sessionid": env_sessionid_ss}

    # Always set tt-target-idc to avoid Akamai EdgeSuite Access Denied on TikTok Native Live API
    if isinstance(cookies, dict):
        cookies.setdefault("tt-target-idc", "useast1a")
    else:
        cookies = {"tt-target-idc": "useast1a"}
        
    return cookies

def save_cookies(cookies: Dict[str, Any]) -> bool:
    """
    Save TikTok cookies to cookies.json.
    
    Args:
        cookies: Cookie dictionary to save
        
    Returns:
        True if successful, False otherwise
    """
    tmp_path = constants.COOKIES_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=4, ensure_ascii=False)
        
        # Atomic replace
        os.replace(tmp_path, constants.COOKIES_FILE)
        return True
        
    except Exception as e:
        print(f"[!] Không ghi được cookies.json: {e}")
        # Clean up temp file
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False