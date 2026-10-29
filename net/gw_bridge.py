#!/usr/bin/env python3
"""Model-gateway bridge for one recorded run (host side, root).

The agent container runs with --network none. Inside it, a root forwarder (image/gw_forward.py)
turns 127.0.0.1:8080 into this bridge's Unix socket, which is mounted in a root-only directory.
The bridge
  * forwards only model API calls (POST /v1/messages, /v1/messages/count_tokens,
    /v1/chat/completions) to the upstream gateway, and only for the allowed model ids;
  * answers GET /v1/models itself, with the allowed ids only;
  * drops client credentials and accept-encoding, and forces one request per connection;
  * writes every request body and the raw upstream response to --out, outside the agent's reach.
    This is a third record: what the harness sent to the model and what the model answered
    (cf. the interception gateway in Section 4 of the paper).

Usage: gw_bridge.py --socket PATH --out DIR [--upstream 127.0.0.1:3425] --allow-model ID [--allow-model ID ...]
Stop with SIGTERM or SIGINT.
"""
import argparse
import datetime
import json
import os
import signal
import socket
import socketserver
import sys
import threading

FORWARD_PATHS = {"/v1/messages", "/v1/messages/count_tokens", "/v1/chat/completions"}
PASS_HEADERS = {"content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent"}
MAX_HEAD = 64 * 1024


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def read_head(sock, buf=b""):
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
        if len(buf) > MAX_HEAD and b"\r\n\r\n" not in buf:
            raise ValueError("header too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head.decode("latin-1"), rest


def read_exact(sock, buf, n):
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ValueError("client closed before the body was complete")
        buf += chunk
    return buf[:n]


def read_chunked(sock, buf):
    body = b""
    while True:
        while b"\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ValueError("client closed inside a chunked body")
            buf += chunk
        line, buf = buf.split(b"\r\n", 1)
        size = int(line.split(b";")[0].strip(), 16)
        while len(buf) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                raise ValueError("client closed inside a chunk")
            buf += chunk
        if size == 0:
            return body
        body += buf[:size]
        buf = buf[size + 2:]


class Bridge(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, out, upstream, allow, timeout):
        self.out, self.allow, self.timeout = out, list(allow), timeout
        host, _, port = upstream.rpartition(":")
        self.up_host, self.up_port = host, int(port)
        self.lock = threading.Lock()
        self.count = 0
        os.makedirs(os.path.join(out, "bodies"), mode=0o700, exist_ok=True)
        self.logf = open(os.path.join(out, "gw.jsonl"), "a", buffering=1)
        super().__init__(path, Handler)
        os.chmod(path, 0o600)

    def next_id(self):
        with self.lock:
            self.count += 1
            return self.count

    def log(self, rec):
        with self.lock:
            self.logf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def body_path(self, n, suffix):
        return os.path.join(self.out, "bodies", f"{n:05d}.{suffix}")


class Handler(socketserver.BaseRequestHandler):
    def reply(self, rec, status, obj):
        data = json.dumps(obj).encode()
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 502: "Bad Gateway"}.get(status, "Error")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n")
        self.request.sendall(head.encode() + data)
        rec.update(status=status, resp_bytes=len(data), answered_by="bridge")

    def handle(self):
        srv, c = self.server, self.request
        c.settimeout(srv.timeout)
        rec = {"t_start": utc()}
        try:
            head, rest = read_head(c)
            if head is None:
                return  # connection opened and closed without a request (readiness probe)
            rec["n"] = n = srv.next_id()
            lines = head.split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = []
            for line in lines[1:]:
                k, _, v = line.partition(":")
                headers.append((k.strip(), v.strip()))
            low = {k.lower(): v for k, v in headers}
            path = target.split("?", 1)[0]
            rec.update(method=method, target=target, user_agent=low.get("user-agent"))
            if "chunked" in low.get("transfer-encoding", "").lower():
                body = read_chunked(c, rest)
            else:
                body = read_exact(c, rest, int(low.get("content-length") or 0))
            rec["req_bytes"] = len(body)
            model = None
            if body:
                try:
                    model = json.loads(body).get("model")
                except (ValueError, AttributeError):
                    model = None
            rec["model"] = model
            if body:
                with open(srv.body_path(n, "req.json"), "wb") as f:
                    f.write(body)
            if method == "GET" and path == "/v1/models":
                return self.reply(rec, 200, {"object": "list", "data": [
                    {"id": m, "object": "model", "type": "model", "display_name": m, "owned_by": "gateway"}
                    for m in srv.allow]})
            if method != "POST" or path not in FORWARD_PATHS:
                return self.reply(rec, 404, {"error": {"type": "not_found_error",
                                                       "message": "not available through this gateway"}})
            if model not in srv.allow:
                return self.reply(rec, 403, {"error": {"type": "permission_error",
                                                       "message": f"model {model!r} is not available here"}})
            out = [f"{method} {target} HTTP/1.1", f"Host: {srv.up_host}:{srv.up_port}",
                   "Connection: close", f"Content-Length: {len(body)}"]
            out += [f"{k}: {v}" for k, v in headers if k.lower() in PASS_HEADERS]
            try:
                u = socket.create_connection((srv.up_host, srv.up_port), timeout=srv.timeout)
            except OSError as e:
                rec["error"] = f"upstream connect: {e!r}"
                return self.reply(rec, 502, {"error": {"type": "api_error", "message": "upstream unavailable"}})
            with u, open(srv.body_path(n, "resp.http"), "wb") as f:
                u.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
                rhead, rrest = read_head(u)
                if rhead is None:
                    rec["error"] = "upstream closed without a response"
                    return self.reply(rec, 502, {"error": {"type": "api_error", "message": "empty upstream reply"}})
                rlines = rhead.split("\r\n")
                rec["status"] = int(rlines[0].split(" ")[1])
                kept = [ln for ln in rlines[1:] if ln.split(":", 1)[0].strip().lower()
                        not in ("connection", "keep-alive")]
                new_head = "\r\n".join([rlines[0]] + kept + ["Connection: close"]) + "\r\n\r\n"
                f.write(rhead.encode("latin-1") + b"\r\n\r\n")
                c.sendall(new_head.encode("latin-1") + rrest)
                f.write(rrest)
                total = len(rrest)
                while True:
                    chunk = u.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    c.sendall(chunk)
                rec.update(resp_bytes=total, answered_by="upstream")
        except Exception as e:  # keep the bridge alive; the record says what went wrong
            rec["error"] = repr(e)
        finally:
            if "n" in rec:
                rec["t_end"] = utc()
                srv.log(rec)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--socket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--upstream", default="127.0.0.1:3425")
    ap.add_argument("--allow-model", action="append", required=True)
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args()
    if os.path.exists(a.socket):
        os.unlink(a.socket)
    old = os.umask(0o077)
    srv = Bridge(a.socket, a.out, a.upstream, a.allow_model, a.timeout)
    os.umask(old)

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    srv.log({"t_start": utc(), "event": "bridge_start", "upstream": a.upstream, "allow": a.allow_model})
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        srv.log({"t_start": utc(), "event": "bridge_stop", "requests": srv.count})
        srv.server_close()
        if os.path.exists(a.socket):
            os.unlink(a.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
