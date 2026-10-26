#!/usr/bin/env python3
"""TCP forwarder in front of the stress CT, for the multi-host run: devices on networks that cannot reach the
CT reach Headscale (control and its embedded DERP relay, both on one HTTPS port) through this machine.
Stands in for the reverse proxy in front of the real server (nginx-proxy-manager on host-c).

    fwd.py <listen-ip>:<port> <target-ip>:<port> [status-file]

Writes the open-connection count to the status file every 5 s.
"""

from __future__ import annotations

import asyncio
import json
import resource
import sys
import time

open_now = total = 0


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        w.close()


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter, host: str, port: int) -> None:
    global open_now, total
    try:
        tr, tw = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
    except (OSError, asyncio.TimeoutError):
        cw.close()
        return
    open_now += 1
    total += 1
    try:
        await asyncio.gather(pipe(cr, tw), pipe(tr, cw))
    finally:
        open_now -= 1


async def status(path: str) -> None:
    while True:
        with open(path, "w") as f:
            json.dump({"at": time.time(), "open": open_now, "total": total}, f)
        await asyncio.sleep(5)


async def main(listen: str, target: str, status_file: str | None) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    lh, lp = listen.rsplit(":", 1)
    th, tp = target.rsplit(":", 1)
    server = await asyncio.start_server(lambda r, w: handle(r, w, th, int(tp)), lh, int(lp), backlog=4096)
    if status_file:
        asyncio.create_task(status(status_file))
    print(f"forwarding {listen} -> {target} (open files limit {hard})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
