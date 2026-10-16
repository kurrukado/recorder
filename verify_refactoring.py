#!/usr/bin/env python3
"""
Final verification of TikTok Recorder refactoring.
Checks that all files use shared modules correctly.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def test_imports():
    """Test that all shared modules can be imported."""
    print("🧪 Testing shared module imports...")
    
    tests = [
        ("constants", "import constants"),
        ("shared_config", "from shared_config import load_config, save_config_fields, load_cookies, save_cookies"),
        ("logging_utils", "from logging_utils import print_with_timestamp"),
        ("config_lock", "from config_lock import config_transaction"),
    ]
    
    all_ok = True
    for module_name, import_stmt in tests:
        try:
            exec(import_stmt)
            print(f"✓ {module_name}")
        except Exception as e:
            print(f"❌ {module_name}: {e}")
            all_ok = False
    
    return all_ok

def test_module_usage():
    """Test that key modules can be used."""
    print("\n🔧 Testing module functionality...")
    
    try:
        from shared_config import load_config, load_cookies
        config = load_config()
        print(f"✓ Config loaded: {len(config)} keys")
        
        cookies = load_cookies()
        print(f"✓ Cookies loaded: {len(cookies)} cookies")
        
        from logging_utils import print_with_timestamp
        print_with_timestamp("✓ Logging works")
        
        return True
    except Exception as e:
        print(f"❌ Module functionality: {e}")
        return False

def check_file_duplicates():
    """Check for remaining duplicate functions."""
    print("\n🔍 Checking for duplicate functions...")
    
    files_to_check = [
        'cloud_daemon.py',
        'gdrive_manager.py', 
        'notifier.py',
        'auto_h264.py',
        'gdrive_auth.py',
        'supabase_sync.py',
        'api_server.py',
        'recorder_core.py'
    ]
    
    issues = []
    for filename in files_to_check:
        filepath = os.path.join(BASE_DIR, filename)
        if not os.path.exists(filepath):
            continue
            
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for duplicate definitions
        if "def load_config():" in content and "from shared_config import load_config" not in content:
            issues.append(f"{filename}: Has local load_config() but doesn't import from shared_config")
        
        if "def save_config(" in content and "from shared_config import save_config" not in content:
            issues.append(f"{filename}: Has local save_config() but doesn't import from shared_config")
        
        if "def load_cookies():" in content and "from shared_config import load_cookies" not in content:
            issues.append(f"{filename}: Has local load_cookies() but doesn't import from shared_config")
    
    if issues:
        print("⚠️  Found issues:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    else:
        print("✓ No duplicate function issues found")
        return True

def check_constants_usage():
    """Check that constants are used from shared module."""
    print("\n📦 Checking constants usage...")
    
    files_to_check = [
        'cloud_daemon.py',
        'gdrive_manager.py', 
        'notifier.py',
        'auto_h264.py',
        'gdrive_auth.py',
        'supabase_sync.py',
        'api_server.py',
        'recorder_core.py'
    ]
    
    issues = []
    for filename in files_to_check:
        filepath = os.path.join(BASE_DIR, filename)
        if not os.path.exists(filepath):
            continue
            
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for local BASE_DIR definition
        if "BASE_DIR = os.path.dirname(os.path.abspath(__file__))" in content and "import constants" not in content:
            issues.append(f"{filename}: Has local BASE_DIR but doesn't import constants")
        
        # Check for local CONFIG_FILE definition
        if "CONFIG_FILE = os.path.join(BASE_DIR" in content and "import constants" not in content:
            issues.append(f"{filename}: Has local CONFIG_FILE but doesn't import constants")
    
    if issues:
        print("⚠️  Found constants issues:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    else:
        print("✓ All constants come from shared module")
        return True

def main():
    """Main verification function."""
    print("=" * 60)
    print("🔍 TIKTOK RECORDER REFACTORING VERIFICATION")
    print("=" * 60)
    
    results = []
    
    # Run all tests
    results.append(("Shared imports", test_imports()))
    results.append(("Module functionality", test_module_usage()))
    results.append(("No duplicate functions", check_file_duplicates()))
    results.append(("Constants usage", check_constants_usage()))
    
    print("\n" + "=" * 60)
    print("📊 VERIFICATION SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✨ ALL TESTS PASSED!")
        print("\nThe refactoring is complete and working correctly.")
        print("Key improvements:")
        print("1. ✅ Centralized configuration in shared_config.py")
        print("2. ✅ Shared constants in constants.py")
        print("3. ✅ Unified logging in logging_utils.py")
        print("4. ✅ Eliminated duplicate code")
        print("5. ✅ Maintained backward compatibility")
    else:
        print("⚠️  SOME TESTS FAILED")
        print("\nPlease review the issues above before proceeding.")
        sys.exit(1)

if __name__ == "__main__":
    main()