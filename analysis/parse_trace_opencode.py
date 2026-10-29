#!/usr/bin/env python3
"""Extract tool-call claims from an OpenCode native trace (SQLite: ~/.local/share/opencode/opencode.db).

Tool calls are rows of the `part` table whose JSON `data` has type "tool":
  {"tool": "bash"|"write"|"edit"|"read"|..., "callID", "state": {"status", "input": {...}, "output",
   "metadata": {"exit": int, ...}, "time": {"start": ms, "end": ms}}}
(shape checked on OpenCode 1.19.x, 2026-10-28). The database is copied together with its -wal/-shm files
into a temporary directory first, so the original is never opened and the WAL content is included.
SKELETON: the legacy JSON store (~/.local/share/opencode/storage/) is not parsed.

Claim (dict): same fields as parse_trace_claude.py.
Usage: parse_trace_opencode.py <dir containing opencode.db>   prints JSON lines
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

PATH_KEYS = ("filePath", "file_path", "path")


def parse_db_dir(d):
    db = os.path.join(d, "opencode.db")
    if not os.path.exists(db):
        return []
    tmp = tempfile.mkdtemp(prefix="mx-oc-")
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                shutil.copy2(db + suffix, os.path.join(tmp, "opencode.db" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "opencode.db"))
        try:
            rows = con.execute("select id, session_id, time_created, data from part order by time_created, id").fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    claims = []
    for pid, sid, tcreated, data in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if d.get("type") == "tool":
            claims.append(claim_from_part(d, pid, sid, tcreated, db))
    return claims


def claim_from_part(d, pid, sid, tcreated, src):
    st = d.get("state") or {}
    inp = st.get("input") or {}
    tm = st.get("time") or {}
    meta = st.get("metadata") or {}
    tool = d.get("tool")
    return {
        "id": d.get("callID") or pid, "part_id": pid, "session": sid, "tool": tool,
        "t_use": (tm.get("start") or tcreated) / 1000.0 if (tm.get("start") or tcreated) else None,
        "t_result": tm["end"] / 1000.0 if tm.get("end") else None,
        "input": inp, "command": inp.get("command") if tool == "bash" else None,
        "paths": [inp[k] for k in PATH_KEYS if isinstance(inp.get(k), str)],
        "reported": {"status": st.get("status"), "output": st.get("output"),
                     "exit": meta.get("exit"),
                     "is_error": (st.get("status") == "error") or (isinstance(meta.get("exit"), int) and meta["exit"] != 0)},
        "file": src,
    }


def parse_stdout(path):
    """Claims from `opencode run --format json` output (one JSON event per line; tool parts in "tool_use")."""
    out, seen = [], set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            part = e.get("part") if isinstance(e, dict) else None
            if e.get("type") != "tool_use" or not isinstance(part, dict) or part.get("type") != "tool":
                continue
            cl = claim_from_part(part, part.get("id"), part.get("sessionID"), e.get("timestamp"), path)
            if cl["id"] not in seen:
                seen.add(cl["id"])
                out.append(cl)
    return out


def parse(paths):
    out, seen = [], set()
    for p in paths:
        for cl in parse_db_dir(p):
            if cl["id"] in seen:
                continue
            seen.add(cl["id"])
            out.append(cl)
    return out


if __name__ == "__main__":
    for cl in parse(sys.argv[1:]):
        print(json.dumps(cl))
