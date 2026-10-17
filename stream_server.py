#!/usr/bin/env python3
"""Live-view web page for the configured RTSP cameras.

Each camera's RTSP stream is transcoded to MJPEG by ffmpeg and served over
HTTP, so any browser can show it in a plain <img> tag. An ffmpeg process is
only started while at least one viewer is watching that camera, and is shared
between all viewers.

Usage:
    ./stream_server.py                 # serve all enabled cameras
    ./stream_server.py front_door      # serve just one camera
    ./stop.sh                          # stops this too (run/*.pid convention)

Then open http://127.0.0.1:8080/ (see stream_bind / stream_port in
cameras.json to change the address).
"""

import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CAMERAS_CONFIG", ROOT_DIR / "cameras.json"))
RUN_DIR = ROOT_DIR / "run"
BOUNDARY = "kukuframe"
IDLE_STOP_SECONDS = 10  # keep ffmpeg alive briefly after the last viewer leaves
RESTART_DELAY_SECONDS = 5


def log(cam_name, msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{cam_name}] {msg}", flush=True)


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(
            f"config file not found: {CONFIG_FILE} "
            "(copy cameras.json.example to cameras.json and edit it)"
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def select_cameras(config, filters):
    cameras = [c for c in config.get("cameras", []) if c.get("enabled", True)]
    if not filters or filters == ["all"]:
        return cameras
    by_name = {c["name"]: c for c in cameras}
    selected = []
    for name in filters:
        if name not in by_name:
            sys.exit(f"no enabled camera named '{name}' in {CONFIG_FILE}")
        selected.append(by_name[name])
    return selected


def resolve_password(cam):
    # Same rules as resolve_password() in lib/common.sh.
    env_var = cam.get("password_env")
    if env_var:
        password = os.environ.get(env_var)
        if not password:
            sys.exit(
                f"environment variable '{env_var}' is not set "
                f"(required for camera {cam['name']})"
            )
        return password
    password = cam.get("password")
    if not password:
        sys.exit(f"camera {cam['name']} has no password or password_env set")
    return password


def build_rtsp_url(cam):
    host = cam.get("host")
    if not host:
        sys.exit(f"camera {cam['name']} is missing 'host'")
    port = cam.get("port", 554)
    # stream_path lets the live view use a lighter substream (e.g. Tapo
    # /stream2) while record.sh keeps recording the main stream.
    path = cam.get("stream_path") or cam.get("path") or "/"
    user = quote(cam.get("username", ""), safe="")
    password = quote(resolve_password(cam), safe="")
    return f"rtsp://{user}:{password}@{host}:{port}{path}"


class CameraStream:
    """One shared ffmpeg -> MJPEG pipeline per camera, started on demand."""

    def __init__(self, cam, config):
        self.name = cam["name"]
        self.input_args = [
            "-rtsp_transport", config.get("rtsp_transport", "tcp"),
            "-timeout", "15000000",
            "-i", build_rtsp_url(cam),
        ]
        fps = cam.get("stream_fps", config.get("stream_fps", 5))
        width = cam.get("stream_width", config.get("stream_width", 1280))
        self.output_args = [
            "-an",
            "-vf", f"fps={fps},scale='min({width},iw)':-2",
            "-q:v", "6",
            "-f", "mjpeg", "-",
        ]
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self.viewers = 0
        self.last_viewer_left = 0.0
        self.proc = None
        threading.Thread(target=self._supervise, daemon=True).start()

    def subscribe(self):
        with self.cond:
            self.viewers += 1
            self.cond.notify_all()

    def unsubscribe(self):
        with self.cond:
            self.viewers -= 1
            if self.viewers == 0:
                self.last_viewer_left = time.monotonic()

    def wait_frame(self, last_id, timeout):
        """Block until a frame newer than last_id exists; returns (id, jpeg)."""
        with self.cond:
            ready = self.cond.wait_for(
                lambda: self.frame is not None and self.frame_id != last_id, timeout
            )
            if not ready:
                return last_id, None
            return self.frame_id, self.frame

    def _wanted(self):
        return self.viewers > 0 or (
            time.monotonic() - self.last_viewer_left < IDLE_STOP_SECONDS
        )

    def _supervise(self):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.viewers > 0)
            self._run_ffmpeg()
            with self.cond:
                self.frame = None
                if self.viewers > 0:
                    log(self.name, f"ffmpeg exited, restarting in {RESTART_DELAY_SECONDS}s")
                    self.cond.wait(RESTART_DELAY_SECONDS)

    def _run_ffmpeg(self):
        log(self.name, "starting ffmpeg")
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             *self.input_args, *self.output_args],
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        stopper = threading.Thread(target=self._stop_when_idle, daemon=True)
        stopper.start()
        buf = b""
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                # ffmpeg's MJPEG output has no embedded thumbnails, and 0xFF
                # bytes in entropy-coded data are stuffed, so SOI/EOI markers
                # reliably delimit frames.
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        break
                    jpeg = buf[start:end + 2]
                    buf = buf[end + 2:]
                    with self.cond:
                        self.frame = jpeg
                        self.frame_id += 1
                        self.cond.notify_all()
        finally:
            self.proc.wait()
            self.proc = None
            log(self.name, "ffmpeg stopped")

    def _stop_when_idle(self):
        proc = self.proc
        while proc and proc.poll() is None:
            with self.cond:
                if not self._wanted():
                    log(self.name, "no viewers, stopping ffmpeg")
                    proc.terminate()
                    return
            time.sleep(1)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kuku-cam-spy</title>
<style>
  body {{ margin: 0; background: #111; color: #ddd; font-family: system-ui, sans-serif; }}
  header {{ padding: 10px 16px; font-weight: 600; }}
  main {{ display: grid; gap: 8px; padding: 0 8px 8px;
          grid-template-columns: repeat(auto-fit, minmax(min(480px, 100%), 1fr)); }}
  figure {{ margin: 0; background: #000; position: relative; cursor: zoom-in; }}
  figure img {{ display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: contain; }}
  figcaption {{ position: absolute; top: 6px; left: 8px; padding: 2px 6px;
                background: rgba(0,0,0,.6); border-radius: 4px; font-size: 14px; }}
  figure:fullscreen {{ cursor: zoom-out; display: flex; align-items: center; }}
  figure:fullscreen img {{ height: 100%; aspect-ratio: auto; }}
</style>
</head>
<body>
<header>kuku-cam-spy — live</header>
<main>
{tiles}
</main>
<script>
  for (const fig of document.querySelectorAll("figure")) {{
    const img = fig.querySelector("img");
    // Reconnect if the stream drops (camera reboot, server restart, ...).
    img.addEventListener("error", () => setTimeout(() => {{
      img.src = img.dataset.src + "?t=" + Date.now();
    }}, 3000));
    fig.addEventListener("click", () => document.fullscreenElement
      ? document.exitFullscreen() : fig.requestFullscreen());
  }}
</script>
</body>
</html>
"""

TILE = """<figure><img src="/stream/{name}" data-src="/stream/{name}" alt="{name}"><figcaption>{name}</figcaption></figure>"""


def make_handler(streams):
    page = PAGE.format(
        tiles="\n".join(TILE.format(name=html.escape(n, quote=True)) for n in streams)
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path.startswith("/stream/") and path[8:] in streams:
                self._stream(streams[path[8:]])
            elif path.startswith("/snapshot/") and path.endswith(".jpg") \
                    and path[10:-4] in streams:
                self._snapshot(streams[path[10:-4]])
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _snapshot(self, stream):
            stream.subscribe()
            try:
                _, jpeg = stream.wait_frame(-1, 15)
            finally:
                stream.unsubscribe()
            if jpeg is None:
                self._send(504, "text/plain", b"camera did not deliver a frame\n")
            else:
                self._send(200, "image/jpeg", jpeg)

        def _stream(self, stream):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            stream.subscribe()
            last_id = 0
            try:
                while True:
                    last_id, jpeg = stream.wait_frame(last_id, 30)
                    if jpeg is None:
                        continue
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                        + jpeg + b"\r\n"
                    )
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stream.unsubscribe()

    return Handler


def main():
    config = load_config()
    filters = sys.argv[1:] or ["all"]
    cameras = select_cameras(config, filters)
    if not cameras:
        sys.exit(f"no matching enabled camera(s) found for: {' '.join(filters)}")

    streams = {cam["name"]: CameraStream(cam, config) for cam in cameras}
    bind = config.get("stream_bind", "127.0.0.1")
    port = int(config.get("stream_port", 8080))
    server = ThreadingHTTPServer((bind, port), make_handler(streams))
    server.daemon_threads = True

    RUN_DIR.mkdir(exist_ok=True)
    pid_file = RUN_DIR / "stream_server.pid"
    pid_file.write_text(str(os.getpid()))

    def handle_signal(signum, frame):
        log("main", "stopping...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log("main", f"serving {', '.join(streams)} on http://{bind}:{port}/")
    try:
        server.serve_forever()
    finally:
        for stream in streams.values():
            if stream.proc:
                stream.proc.terminate()
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
