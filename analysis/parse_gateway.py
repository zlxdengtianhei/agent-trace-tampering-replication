#!/usr/bin/env python3
"""Read the gateway bridge's record of one run (gw/gw.jsonl and gw/bodies/).

For every model call the bridge forwarded, returns the request summary and the tool calls the model
asked for, taken from the raw response (Anthropic messages or OpenAI chat completions, streamed or
not). The tool-call ids are the ones the harness stores in its trace (Claude Code `tool_use.id`,
OpenCode `callID`), so the trace can be joined with what the model really requested.

Usage: parse_gateway.py <run_dir>   prints one JSON object per model call
"""
import json
import os
import re
import sys


def dechunk(body):
    out, i = b"", 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            return out + body[i:]
        try:
            size = int(body[i:j].split(b";")[0].strip(), 16)
        except ValueError:
            return out + body[i:]
        if size == 0:
            return out
        out += body[j + 2:j + 2 + size]
        i = j + 2 + size + 2


def split_http(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    hdr = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdr[k.strip().lower()] = v.strip()
    if "chunked" in hdr.get("transfer-encoding", "").lower():
        body = dechunk(body)
    try:
        status = int(lines[0].split(" ")[1])
    except (IndexError, ValueError):
        status = None
    return status, hdr, body


def sse_events(body):
    for block in re.split(rb"\r?\n\r?\n", body):
        data = [ln[5:].strip() for ln in block.split(b"\n") if ln.startswith(b"data:")]
        if not data or data == [b"[DONE]"]:
            continue
        try:
            yield json.loads(b"\n".join(data))
        except ValueError:
            continue


def _args(s, fallback=None):
    if not s:
        return fallback if fallback is not None else {}
    try:
        return json.loads(s)
    except ValueError:
        return s


def from_anthropic(objs):
    calls, text, stop = {}, [], None
    for e in objs:
        t = e.get("type")
        if t == "message":  # non-streamed response
            for i, c in enumerate(e.get("content") or []):
                if c.get("type") == "tool_use":
                    calls[i] = {"id": c.get("id"), "name": c.get("name"), "input": c.get("input")}
                elif c.get("type") == "text":
                    text.append(c.get("text") or "")
            stop = e.get("stop_reason")
        elif t == "content_block_start":
            cb = e.get("content_block") or {}
            if cb.get("type") == "tool_use":
                calls[e.get("index")] = {"id": cb.get("id"), "name": cb.get("name"), "_json": "", "input": cb.get("input")}
        elif t == "content_block_delta":
            d = e.get("delta") or {}
            if d.get("type") == "input_json_delta" and e.get("index") in calls:
                calls[e["index"]]["_json"] += d.get("partial_json") or ""
            elif d.get("type") == "text_delta":
                text.append(d.get("text") or "")
        elif t == "message_delta":
            stop = (e.get("delta") or {}).get("stop_reason") or stop
    out = []
    for _, c in sorted(calls.items(), key=lambda kv: kv[0] if isinstance(kv[0], int) else 0):
        if "_json" in c:
            c["input"] = _args(c.pop("_json"), c.get("input"))
        out.append(c)
    return out, "".join(text), stop


def from_openai(objs):
    calls, text, stop = {}, [], None
    for e in objs:
        for ch in e.get("choices") or []:
            msg = ch.get("message")
            if msg:  # non-streamed response
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    calls[i] = {"id": tc.get("id"), "name": fn.get("name"), "_json": fn.get("arguments") or ""}
                text.append(msg.get("content") or "")
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": None, "name": None, "_json": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] = fn.get("name") or c["name"]
                c["_json"] += fn.get("arguments") or ""
            text.append((ch.get("delta") or {}).get("content") or "")
            stop = ch.get("finish_reason") or stop
    out = []
    for _, c in sorted(calls.items()):
        c["input"] = _args(c.pop("_json"))
        out.append(c)
    return out, "".join(text), stop


def load(run):
    gw = os.path.join(run, "gw")
    path = os.path.join(gw, "gw.jsonl")
    if not os.path.exists(path):
        return None
    calls = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "n" not in r:
                continue
            rec = {k: r.get(k) for k in ("n", "t_start", "t_end", "method", "target", "model", "status",
                                         "answered_by", "error")}
            rec["tool_calls"], rec["text"], rec["stop"] = [], "", None
            req_p = os.path.join(gw, "bodies", f"{r['n']:05d}.req.json")
            if os.path.exists(req_p):
                try:
                    with open(req_p, "rb") as g:
                        req = json.loads(g.read())
                    rec["stream"] = bool(req.get("stream"))
                    rec["messages_sent"] = len(req.get("messages") or [])
                except ValueError:
                    pass
            resp_p = os.path.join(gw, "bodies", f"{r['n']:05d}.resp.http")
            if r.get("answered_by") == "upstream" and os.path.exists(resp_p):
                with open(resp_p, "rb") as g:
                    _, hdr, body = split_http(g.read())
                if "text/event-stream" in hdr.get("content-type", ""):
                    objs = list(sse_events(body))
                else:
                    try:
                        objs = [json.loads(body)]
                    except ValueError:
                        objs = []
                fmt = "openai" if "/chat/completions" in (r.get("target") or "") else "anthropic"
                rec["tool_calls"], rec["text"], rec["stop"] = (from_openai if fmt == "openai" else from_anthropic)(objs)
            calls.append(rec)
    return calls


if __name__ == "__main__":
    for c in load(sys.argv[1]) or []:
        print(json.dumps(c, ensure_ascii=False))
