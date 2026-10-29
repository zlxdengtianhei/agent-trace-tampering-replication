#!/usr/bin/env python3
"""Before/after snapshot of an agent run's home and workspace (host side, outside the container).

Writes <out>/manifest.json (relative path -> size, sha256, mode, mtime) and a copy of every
regular file up to MAX_FILE bytes under <out>/files/. Caches are skipped.
Usage: snapshot.py <work_dir> <out_dir>      work_dir contains home/ and workspace/
"""
import hashlib
import json
import os
import shutil
import stat
import sys

MAX_FILE = 16 * 1024 * 1024
SKIP_DIRS = {".npm", ".cache", "node_modules", ".bun"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    work, out = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.join(out, "files"), exist_ok=True)
    manifest = {}
    for top in ("home", "workspace"):
        base = os.path.join(work, top)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, work)
                try:
                    st = os.lstat(full)
                except FileNotFoundError:
                    continue
                entry = {"size": st.st_size, "mode": oct(st.st_mode), "mtime": st.st_mtime, "inode": st.st_ino}
                if stat.S_ISREG(st.st_mode):
                    try:
                        entry["sha256"] = sha256(full)
                        if st.st_size <= MAX_FILE:
                            dst = os.path.join(out, "files", rel)
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(full, dst)
                    except OSError as e:
                        entry["error"] = str(e)
                elif stat.S_ISLNK(st.st_mode):
                    entry["link"] = os.readlink(full)
                manifest[rel] = entry
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    print(f"snapshot: {len(manifest)} files -> {out}")


if __name__ == "__main__":
    main()
