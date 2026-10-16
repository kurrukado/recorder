#!/usr/bin/env python3
"""
Simple script to check and update one file at a time.
"""

import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def update_cloud_daemon():
    """Update cloud_daemon.py to use shared modules."""
    filepath = os.path.join(BASE_DIR, "cloud_daemon.py")
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Add imports at the top
    import_lines = [
        "# Import shared modules",
        "import constants",
        "from shared_config import load_config, save_config, save_config_fields",
        "from logging_utils import print_with_timestamp, print_error, print_success",
        ""
    ]
    
    lines = content.split('\n')
    
    # Find where to insert imports (after existing imports)
    insert_pos = 0
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith(('import ', 'from ', '#', '"', "'")) and not line.strip().startswith('"""'):
            insert_pos = i
            break
    
    # Insert imports
    for import_line in reversed(import_lines):
        lines.insert(insert_pos, import_line)
    
    # Replace BASE_DIR and CONFIG_FILE
    for i, line in enumerate(lines):
        if "BASE_DIR = os.path.dirname" in line:
            lines[i] = "# BASE_DIR is imported from constants"
        elif "CONFIG_FILE = os.path.join" in line:
            lines[i] = "# CONFIG_FILE is imported from constants"
    
    # Remove duplicate load_config function
    in_load_config = False
    load_config_start = -1
    for i, line in enumerate(lines):
        if "def load_config():" in line:
            load_config_start = i
            in_load_config = True
        elif in_load_config and line.strip() == "":
            # Found empty line, end of function
            for j in range(load_config_start, i):
                lines[j] = "# load_config() is imported from shared_config"
            in_load_config = False
    
    # Remove duplicate save_config function
    in_save_config = False
    save_config_start = -1
    for i, line in enumerate(lines):
        if "def save_config(cfg):" in line:
            save_config_start = i
            in_save_config = True
        elif in_save_config and line.strip() == "" and i > save_config_start + 15:
            # Found empty line well after function start
            for j in range(save_config_start, i):
                lines[j] = "# save_config() is imported from shared_config"
            in_save_config = False
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write('\n'.join(lines))
    
    print("✅ Updated cloud_daemon.py")

def test_updates():
    """Test that the updates work."""
    print("\n🧪 Testing imports...")
    
    try:
        import constants
        print("✓ constants")
    except Exception as e:
        print(f"❌ constants: {e}")
        return False
    
    try:
        from shared_config import load_config, save_config
        print("✓ shared_config")
    except Exception as e:
        print(f"❌ shared_config: {e}")
        return False
    
    try:
        from logging_utils import print_with_timestamp
        print("✓ logging_utils")
    except Exception as e:
        print(f"❌ logging_utils: {e}")
        return False
    
    print("\n✅ All shared modules work!")
    return True

if __name__ == "__main__":
    print("🔄 Updating cloud_daemon.py...")
    update_cloud_daemon()
    
    if test_updates():
        print("\n✨ Update completed successfully!")
    else:
        print("\n⚠️ Some tests failed. Please check manually.")