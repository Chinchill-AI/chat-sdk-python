"""One-time script to copy fixture files from TS repo.

Run: python tests/fixtures/copy_fixtures.py
"""

import os
import shutil
import sys

SRC = "/tmp/vercel-chat/packages/integration-tests/fixtures/replay"
DST = os.path.join(os.path.dirname(__file__), "replay")


def main():
    if not os.path.isdir(SRC):
        print(f"Source not found: {SRC}", file=sys.stderr)
        sys.exit(1)

    count = 0
    for root, _dirs, files in os.walk(SRC):
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            rel = os.path.relpath(os.path.join(root, fname), SRC)
            dst_path = os.path.join(DST, rel)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(os.path.join(root, fname), dst_path)
            count += 1
            print(f"  Copied: {rel}")

    print(f"\nCopied {count} fixture files to {DST}")


if __name__ == "__main__":
    main()
