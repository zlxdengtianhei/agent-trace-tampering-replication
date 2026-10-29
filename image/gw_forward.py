#!/usr/bin/env python3
"""Inside the container, as root: forward 127.0.0.1:<port> to the host bridge's Unix socket.

The container has no network interface except loopback (--network none). This forwarder only
moves bytes; the host-side bridge (net/gw_bridge.py) decides what reaches the model gateway and
records it. The socket sits in a root-only directory, so the agent (uid 23001) can reach the
gateway only through this port.

Usage: gw_forward.py /run/mx-gw/gw.sock 8080
"""
import asyncio
import sys

SOCK = sys.argv[1]
PORT = int(sys.argv[2])


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(client_r, client_w):
    try:
        up_r, up_w = await asyncio.open_unix_connection(SOCK)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"forwarding 127.0.0.1:{PORT} -> {SOCK}", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
