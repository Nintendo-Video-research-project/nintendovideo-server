#!/usr/bin/env python3
"""Nintendo Video SpotPass server.

Handles, regardless of Host header:
  .../policylist...  or /p01/...     generated policylist XML
  .../CHECK                          app connectivity check
  .../{PREFIX}{N}  (e.g. ESE_MD1)    BOSS container for that task (Range supported)
  POST anything, /                   200 OK (reports, log uploads, auto-connect)
"""
import argparse
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import library

log = logging.getLogger("nintendovideo")

PUBLIC_URL = "https://video.mariocube.com"
TASK_RE = re.compile(r"/([A-Z]{3}_MD[1-9])$")
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def policylist() -> bytes:
    by_title: dict[int, list[str]] = {}
    for task in library.TASKS:
        by_title.setdefault(library.REGIONS[task[:-1]], []).append(task)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<policylist version="1">']
    for title_id, tasks in by_title.items():
        out.append(f'    <title id="{title_id:016X}">')
        for prio, task in enumerate(tasks, 1):
            out += [f'        <task id="{task}">',
                    f"            <url>{PUBLIC_URL}/1/1/1/{task}</url>",
                    "            <interval>1</interval>",
                    "            <retry_interval>10</retry_interval>",
                    f"            <priority>{(prio - 1) % len(library.SLOTS) + 1}</priority>",
                    "            <opt_out>false</opt_out>",
                    "        </task>"]
        out.append("    </title>")
    out.append("</policylist>")
    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Nintendo"
    sys_version = ""

    def _client(self) -> str:
        """Real client IP: Caddy puts it in X-Forwarded-For."""
        headers = getattr(self, "headers", None)
        fwd = headers.get("X-Forwarded-For", "") if headers else ""
        return fwd.split(",")[0].strip() or self.client_address[0]

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok:
            h = self.headers
            log.info("<- %s %s %s host=%s ua=%r range=%s len=%s", self._client(), self.command, self.path,
                     h.get("Host", "-"), h.get("User-Agent", "-"), h.get("Range", "-"), h.get("Content-Length", "-"))
        return ok

    def log_message(self, fmt, *args):
        headers = getattr(self, "headers", None)
        log.info("-> %s [%s] %s", self._client(), headers.get("Host", "-") if headers else "-", fmt % args)

    def _reply(self, code: int, body: bytes = b"OK", ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Organization", "Nintendo")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if "policylist" in path or path.startswith(("/p01/", "/nppl/")):
                return self._reply(200, policylist(), "application/xml; charset=utf-8")
            if path.endswith("/CHECK"):
                # Nintendo Video CHECK: 404 = region OK, 403 = region mismatch (3dbrew)
                return self._reply(404, b"Not Found")
            if path == "/" or path.startswith("/AC"):
                return self._reply(200)
            m = TASK_RE.search(path)
            if m:
                return self._send_task(m.group(1))
            log.warning("unhandled %s %s (Host: %s)", self.command, self.path, self.headers.get("Host"))
            self._reply(404, b"Not Found")
        except (BrokenPipeError, ConnectionResetError):
            log.info("%s disconnected", self.client_address[0])

    do_HEAD = do_GET

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply(200)

    def _send_task(self, task: str):
        if task not in library.TASKS:
            return self._reply(404, b"Unknown task")
        f = library.container_for(task)
        if f is None:
            log.warning("no content for %s", task)
            return self._reply(404, b"No content")
        size = f.stat().st_size
        start, end = 0, size - 1
        rng = RANGE_RE.match(self.headers.get("Range", "").strip())
        if rng and (rng[1] or rng[2]):
            if rng[1]:
                start, end = int(rng[1]), min(int(rng[2] or end), end)
            else:
                start = max(size - int(rng[2]), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(int(f.stat().st_mtime)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        log.info("sending %s (%s) bytes %d-%d", task, f.name, start, end)
        sent = 0
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                while sent < length:
                    chunk = fh.read(min(1 << 20, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
        finally:
            log.info("%s %s: sent %d/%d bytes to %s", "done" if sent == length else "ABORTED",
                     task, sent, length, self._client())


def rotator(stop: threading.Event):
    while not stop.is_set():
        try:
            library.tick()
        except Exception:
            log.exception("rotation failed")
        stop.wait(600)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    stop = threading.Event()
    threading.Thread(target=rotator, args=(stop,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log.info("listening on %s:%d", args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
