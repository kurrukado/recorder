#!/usr/bin/env python3
"""
Endpoint analysis for deployment readiness
"""

import json
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def analyze_endpoints():
    """Analyze all endpoints from api_server.py"""
    print("🔍 Analyzing API endpoints from api_server.py...")
    
    endpoints = []
    with open(os.path.join(BASE_DIR, "api_server.py"), "r", encoding="utf-8") as f:
        lines = f.readlines()
        
        for i, line in enumerate(lines):
            # Find endpoint definitions
            if '@app.get(' in line or '@app.post(' in line or '@app.delete(' in line:
                # Extract method and path
                if '@app.get(' in line:
                    method = "GET"
                elif '@app.post(' in line:
                    method = "POST"
                elif '@app.delete(' in line:
                    method = "DELETE"
                else:
                    continue
                
                # Find the path
                if '"/' in line:
                    path_start = line.find('"/') + 1
                    path_end = line.find('"', path_start)
                    if path_end > path_start:
                        path = line[path_start:path_end]
                        
                        # Find function name (next few lines)
                        func_name = ""
                        for j in range(i+1, min(i+10, len(lines))):
                            if 'def ' in lines[j]:
                                func_start = lines[j].find('def ') + 4
                                func_end = lines[j].find('(', func_start)
                                func_name = lines[j][func_start:func_end].strip()
                                break
                        
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "function": func_name
                        })
    
    # Group by path prefix
    endpoint_groups = {}
    for ep in endpoints:
        prefix = ep["path"].split("/")[2] if len(ep["path"].split("/")) > 2 else "other"
        if prefix not in endpoint_groups:
            endpoint_groups[prefix] = []
        endpoint_groups[prefix].append(ep)
    
    print(f"\n📊 Found {len(endpoints)} endpoints:")
    print("=" * 60)
    
    for prefix, group in sorted(endpoint_groups.items()):
        print(f"\n📁 {prefix.upper()} API ({len(group)} endpoints):")
        for ep in sorted(group, key=lambda x: x["path"]):
            print(f"  {ep['method']:6} {ep['path']:30} → {ep['function']}")
    
    return endpoints