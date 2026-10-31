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


def tool_results(req):
    """Tool results the harness sent to the model in one request: {tool-call id: {"output", "is_error"}}
    (is_error: Anthropic format only)."""
    out = {}
    for m in req.get("messages") or []:
        if m.get("role") == "tool":  # OpenAI chat format
            out[m.get("tool_call_id")] = {"output": _text(m.get("content")), "is_error": None}
        elif isinstance(m.get("content"), list):  # Anthropic messages format
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out[b.get("tool_use_id")] = {"output": _text(b.get("content")), "is_error": bool(b.get("is_error"))}
    return out


def load(run):
    gw = os.path.join(run, "gw")
    path = os.path.join(gw, "gw.jsonl")
    if not os.path.exists(path):
        return None
    if not os.path.isdir(os.path.join(gw, "bodies")) and os.path.exists(os.path.join(gw, "tool_calls.jsonl")):
        # Published runs carry gw/tool_calls.jsonl (written by export_run.py) instead of the raw bodies.
        with open(os.path.join(gw, "tool_calls.jsonl")) as f:
            return [json.loads(line) for line in f if line.strip()]
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
            rec["tool_calls"], rec["text"], rec["stop"], rec["tool_results"] = [], "", None, {}
            req_p = os.path.join(gw, "bodies", f"{r['n']:05d}.req.json")
            if os.path.exists(req_p):
                try:
                    with open(req_p, "rb") as g:
                        req = json.loads(g.read())
                    rec["stream"] = bool(req.get("stream"))
                    rec["messages_sent"] = len(req.get("messages") or [])
                    rec["tool_results"] = tool_results(req)
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


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) and "text" in c else
                               (c.get("content") if isinstance(c, dict) else c)) for c in content)
    return "" if content is None else json.dumps(content)


def results_sent(run):
    """{tool-call id: text} of every tool result the harness sent back to the model (first time it was sent),
    read from the request bodies; for published runs, from gw/tool_results_sent.jsonl."""
    gw = os.path.join(run, "gw")
    bodies = os.path.join(gw, "bodies")
    out = {}
    if not os.path.isdir(bodies):
        p = os.path.join(gw, "tool_results_sent.jsonl")
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        out[r["id"]] = r["text"]
        return out
    for name in sorted(os.listdir(bodies)):
        if not name.endswith(".req.json"):
            continue
        try:
            with open(os.path.join(bodies, name), "rb") as f:
                req = json.loads(f.read())
        except ValueError:
            continue
        for m in req.get("messages") or []:
            if m.get("role") == "tool" and m.get("tool_call_id"):  # OpenAI chat completions
                out.setdefault(m["tool_call_id"], _text(m.get("content")))
            elif isinstance(m.get("content"), list):  # Anthropic messages
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                        out.setdefault(b["tool_use_id"], _text(b.get("content")))
    return out


if __name__ == "__main__":
    for c in load(sys.argv[1]) or []:
        print(json.dumps(c, ensure_ascii=False))
