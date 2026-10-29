#!/usr/bin/env python3
"""Shadow recorder for native trace files (host side, outside the container).

Polls the agent's home every INTERVAL seconds and keeps a copy of every version of every
trace-like file, so that content survives even if the agent later deletes or rewrites it.
Writes <out>/index.jsonl: {"t", "tick", "rel", "event": new|changed|deleted, "size", "inode", "sha256", "copy"}.
Stops on SIGTERM/SIGINT after one final poll.

Polling can miss a version that lives shorter than INTERVAL; the kernel observer (bpftrace)
and strace still record the operations themselves. The paper's own observer uses inotify for
the same purpose (github.com/aisa-group/perfect-crime, trace_lab/observer.py); this one is
written independently and only keeps file versions.
Usage: shadow.py --home <host home dir> --out <dir> [--interval 0.5]
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import time

# Native trace locations relative to the agent home (Claude Code, OpenCode, Codex) plus shell startup files,
# which the paper's tool-call spoofing scenario modifies.
PREFIXES = (".claude/projects/", ".claude/history.jsonl", ".claude/shell-snapshots/", ".claude/todos/",
            ".claude/session-env/", ".claude/file-history/", ".claude/debug/",
            ".local/share/opencode/", ".codex/sessions/", ".codex/shell_snapshots/",
            ".bashrc", ".profile", ".bash_profile", ".bash_env")
SKIP_DIRS = {".npm", ".cache", "node_modules", ".bun", "bin"}
MAX_FILE = 32 * 1024 * 1024
stop = False


def on_signal(signum, frame):
    global stop
    stop = True


def watched(rel):
    return rel.startswith(PREFIXES)


def scan(home):
    found = {}
    for dirpath, dirnames, filenames in os.walk(home):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, home)
            if not watched(rel):
                continue
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            found[rel] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=0.5)
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    os.makedirs(os.path.join(a.out, "versions"), exist_ok=True)
    index = open(os.path.join(a.out, "index.jsonl"), "a", buffering=1)
    last, tick, seq = {}, 0, 0
    while True:
        final = stop
        tick += 1
        now = time.time()
        cur = scan(a.home)
        for rel, sig in cur.items():
            if last.get(rel) == sig:
                continue
            seq += 1
            src = os.path.join(a.home, rel)
            dst = os.path.join(a.out, "versions", f"{seq:06d}__" + rel.replace("/", "__"))
            rec = {"t": now, "tick": tick, "rel": rel, "event": "new" if rel not in last else "changed",
                   "size": sig[0], "inode": sig[2]}
            try:
                if sig[0] <= MAX_FILE:
                    shutil.copy2(src, dst)
                    with open(dst, "rb") as f:
                        rec["sha256"] = hashlib.sha256(f.read()).hexdigest()
                    rec["copy"] = os.path.relpath(dst, a.out)
            except OSError as e:
                rec["error"] = str(e)
            index.write(json.dumps(rec) + "\n")
        for rel in set(last) - set(cur):
            index.write(json.dumps({"t": now, "tick": tick, "rel": rel, "event": "deleted"}) + "\n")
        last = cur
        if final:
            break
        time.sleep(a.interval)
    index.close()


if __name__ == "__main__":
    main()
