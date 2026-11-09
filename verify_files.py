#!/usr/bin/env python3
"""
Checks every expected file exists and has content.

Distinguishes:
    MISSING -- file not found
    EMPTY   -- file has 0 bytes (real problem)
    TINY    -- file has 1-10 bytes (warning, not failure; e.g. .python-version)
    OK      -- file has content

v0.7.0: no `unlimited/` entries. Bridge-only distribution.
"""

import sys
from pathlib import Path

EXPECTED = [
    "server.py",
    "model_catalog.py",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    ".python-version",
    "README.md",
    "LICENSE",
    "Makefile",
    "test_imports.py",
    "verify_files.py",
    "start.sh",
    "start.bat",
]

TINY_THRESHOLD = 10

def main() -> int:
    root = Path(__file__).parent
    missing = 0
    empty = 0
    print(f"Verifying {len(EXPECTED)} files under {root}\n")

    for rel in EXPECTED:
        p = root / rel
        if not p.exists():
            print(f"  MISSING  {rel}")
            missing += 1
            continue
        size = p.stat().st_size
        if size == 0:
            print(f"  EMPTY    {rel}  (0 bytes)")
            empty += 1
            continue
        if size < TINY_THRESHOLD:
            print(f"  TINY     {rel}  ({size} bytes) -- ok, just small")
            continue
        print(f"  OK       {rel}  ({size} bytes)")

    print()
    if missing or empty:
        print(f"FAILED: {missing} missing, {empty} empty")
        return 1
    print("ALL FILES PRESENT")
    return 0

if __name__ == "__main__":
    sys.exit(main())
