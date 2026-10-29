#!/usr/bin/env python3
"""Extract tool-call claims from Claude Code native traces (~/.claude/projects/<project>/<session>.jsonl).

A claim is what the trace says the agent did: a tool_use entry (tool, input) and, if present, its
tool_result (what the trace says happened). `api-request-blob` entries repeat request bodies and are skipped.

Claim (dict): id, tool, t_use, t_result, command (Bash), paths (file tools), reported {is_error, output},
              file, line
Usage: parse_trace_claude.py <file.jsonl or dir> ...   prints JSON lines
"""
import datetime
import json
import os
import sys

PATH_KEYS = ("file_path", "notebook_path", "path")


def ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def files_in(arg):
    if os.path.isdir(arg):
        for dirpath, _, names in os.walk(arg):
            for n in sorted(names):
                if n.endswith(".jsonl"):
                    yield os.path.join(dirpath, n)
    elif os.path.exists(arg):
        yield arg


def claims_from_file(path):
    claims, order = {}, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") not in ("assistant", "user"):
                continue
            msg = e.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    cl = {"id": c.get("id"), "tool": c.get("name"), "t_use": ts(e.get("timestamp")),
                          "t_result": None, "input": inp, "file": path, "line": lineno,
                          "command": inp.get("command") if c.get("name") == "Bash" else None,
                          "paths": [inp[k] for k in PATH_KEYS if isinstance(inp.get(k), str)],
                          "reported": None}
                    if cl["id"] not in claims:
                        order.append(cl["id"])
                    claims[cl["id"]] = cl
                elif c.get("type") == "tool_result":
                    cid = c.get("tool_use_id")
                    out = c.get("content")
                    if isinstance(out, list):
                        out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                    rep = {"is_error": bool(c.get("is_error")), "output": out}
                    tur = e.get("toolUseResult")
                    if isinstance(tur, dict):
                        rep.update({k: tur.get(k) for k in ("stdout", "stderr", "interrupted") if k in tur})
                    if cid in claims:
                        claims[cid]["reported"] = rep
                        claims[cid]["t_result"] = ts(e.get("timestamp"))
                    else:  # a result whose tool_use was removed from the trace
                        claims[cid] = {"id": cid, "tool": None, "t_use": None, "t_result": ts(e.get("timestamp")),
                                       "input": None, "file": path, "line": lineno, "command": None, "paths": [],
                                       "reported": rep, "orphan_result": True}
                        order.append(cid)
    return [claims[i] for i in order]


def parse(paths):
    out, seen = [], set()
    for arg in paths:
        for p in files_in(arg):
            for cl in claims_from_file(p):
                if cl["id"] in seen:
                    continue
                seen.add(cl["id"])
                out.append(cl)
    return out


if __name__ == "__main__":
    for cl in parse(sys.argv[1:]):
        print(json.dumps(cl))
