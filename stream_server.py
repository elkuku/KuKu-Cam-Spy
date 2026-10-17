#!/usr/bin/env python3
"""Live-view web page for the configured RTSP cameras.

Each camera's RTSP stream is transcoded to MJPEG by ffmpeg and served over
HTTP, so any browser can show it in a plain <img> tag. An ffmpeg process is
only started while at least one viewer is watching that camera, and is shared
between all viewers.

The page also lists the person-detection events written by person_watch.py
(snapshots and clips) and alerts when a new one starts.

Usage:
    ./stream_server.py                 # serve all enabled cameras
    ./stream_server.py front_door      # serve just one camera
    ./stop.sh                          # stops this too (run/*.pid convention)

Then open http://127.0.0.1:8080/ (see stream_bind / stream_port in
cameras.json to change the address).
"""

import html
import json
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from camlib import (
    EVENTS_DIR, iter_jpeg_frames, load_config, log, rtsp_input_args,
    select_cameras, write_pid_file,
)

BOUNDARY = "kukuframe"
IDLE_STOP_SECONDS = 10  # keep ffmpeg alive briefly after the last viewer leaves
RESTART_DELAY_SECONDS = 5
EVENTS_PER_CAMERA = 20
EVENT_FILE_RE = re.compile(r"^[\w-]+\.(jpg|mp4)$")
CONTENT_TYPES = {"jpg": "image/jpeg", "mp4": "video/mp4"}


class CameraStream:
    """One shared ffmpeg -> MJPEG pipeline per camera, started on demand."""

    def __init__(self, cam, config):
        self.name = cam["name"]
        # stream_path lets the live view use a lighter substream (e.g. Tapo
        # /stream2) while record.sh keeps recording the main stream.
        self.input_args = rtsp_input_args(cam, config, cam.get("stream_path"))
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
        threading.Thread(target=self._stop_when_idle, daemon=True).start()
        try:
            for jpeg in iter_jpeg_frames(self.proc.stdout):
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


def recent_events(cameras):
    """Newest person_watch.py events of each camera, newest first overall."""
    files = []
    for cam in cameras:
        # ids are sortable timestamps
        files.extend(sorted((EVENTS_DIR / cam).glob("*.json"), reverse=True)[:EVENTS_PER_CAMERA])
    files.sort(key=lambda p: p.stem, reverse=True)
    events = []
    for path in files:
        try:
            events.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue  # being written right now; picked up on the next poll
    return events


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kuku-cam-spy</title>
<style>
  :root { --bg: #111; --fg: #ddd; --muted: #888; --card: #1b1b1b; --alert: #e5484d; }
  body { margin: 0; background: var(--bg); color: var(--fg); font-family: system-ui, sans-serif; }
  header { display: flex; align-items: center; gap: 12px; padding: 10px 16px; }
  header h1 { font-size: 16px; margin: 0; flex: 1; }
  button { background: #2a2a2a; color: var(--fg); border: 1px solid #444; border-radius: 6px;
           padding: 4px 10px; font: inherit; font-size: 13px; cursor: pointer; }
  #live { display: grid; gap: 16px 8px; padding: 0 8px 16px; align-items: start;
          grid-template-columns: repeat(auto-fit, minmax(min(480px, 100%), 1fr)); }
  #live figure { margin: 0; background: #000; position: relative; cursor: zoom-in;
                 outline: 3px solid transparent; transition: outline-color .2s; }
  #live figure.alert { outline-color: var(--alert); }
  #live figure img { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: contain; }
  figcaption { position: absolute; top: 6px; left: 8px; padding: 2px 6px;
               background: rgba(0,0,0,.6); border-radius: 4px; font-size: 14px; }
  #live figure.alert figcaption::after { content: " · PERSON"; color: var(--alert); font-weight: 700; }
  #live figure:fullscreen { cursor: zoom-out; display: flex; align-items: center; }
  #live figure:fullscreen img { height: 100%; aspect-ratio: auto; }
  .cam { min-width: 0; }
  .strip { display: flex; gap: 6px; overflow-x: auto; padding: 6px 0 4px; scroll-snap-type: x proximity; }
  .strip .none { color: var(--muted); font-size: 13px; margin: 4px 0; }
  .event { flex: none; width: 168px; background: var(--card); border-radius: 6px; overflow: hidden;
           cursor: pointer; scroll-snap-align: start; }
  .event img { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: #000; }
  .event div { padding: 4px 8px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .event .meta { color: var(--muted); font-size: 12px; padding-top: 0; }
  .event.active { outline: 2px solid var(--alert); outline-offset: -2px; }
  #toast { position: fixed; right: 16px; bottom: 16px; max-width: min(320px, calc(100% - 32px));
           background: var(--alert); color: #fff; border-radius: 8px; padding: 10px 14px;
           box-shadow: 0 4px 16px rgba(0,0,0,.5); cursor: pointer; display: none; }
  dialog { background: #000; border: 0; padding: 0; max-width: 95vw; max-height: 95vh; }
  dialog::backdrop { background: rgba(0,0,0,.8); }
  dialog video, dialog img { display: block; max-width: 95vw; max-height: 90vh; }
  dialog p { margin: 0; padding: 6px 10px; color: var(--fg); font-size: 13px; background: #111; }
</style>
</head>
<body>
<header>
  <h1>kuku-cam-spy</h1>
  <button id="notify-btn" hidden>Enable desktop notifications</button>
</header>
<main id="live">
__TILES__
</main>
<div id="toast"></div>
<dialog id="viewer"><div id="viewer-media"></div><p id="viewer-caption"></p></dialog>
<script>
  const tiles = {};
  for (const cam of document.querySelectorAll("#live .cam")) {
    const fig = cam.querySelector("figure");
    tiles[cam.dataset.cam] = { fig, strip: cam.querySelector(".strip") };
    const img = fig.querySelector("img");
    // Reconnect if the stream drops (camera reboot, server restart, ...).
    img.addEventListener("error", () => setTimeout(() => {
      img.src = img.dataset.src + "?t=" + Date.now();
    }, 3000));
    fig.addEventListener("click", () => document.fullscreenElement
      ? document.exitFullscreen() : fig.requestFullscreen());
  }

  const fileUrl = (e, name) => `/events/${encodeURIComponent(e.camera)}/${encodeURIComponent(name)}`;
  const when = e => new Date(e.start).toLocaleString();
  const duration = e => e.end ? Math.round((new Date(e.end) - new Date(e.start)) / 1000) + "s" : "ongoing";

  const viewer = document.getElementById("viewer");
  viewer.addEventListener("click", ev => { if (ev.target === viewer) viewer.close(); });
  viewer.addEventListener("close", () => document.getElementById("viewer-media").replaceChildren());
  function openEvent(e) {
    const media = document.createElement(e.clip ? "video" : "img");
    media.src = fileUrl(e, e.clip || e.snapshot);
    if (e.clip) { media.controls = true; media.autoplay = true; media.poster = fileUrl(e, e.snapshot); }
    document.getElementById("viewer-media").replaceChildren(media);
    document.getElementById("viewer-caption").textContent =
      `${e.camera} · ${when(e)} · ${duration(e)} · ${e.clip ? "clip" : e.status === "saving" ? "clip being saved…" : "no clip"}`;
    viewer.showModal();
  }

  const toast = document.getElementById("toast");
  let toastTimer;
  function alertEvent(e) {
    toast.textContent = `Person detected on ${e.camera} at ${new Date(e.start).toLocaleTimeString()}`;
    toast.onclick = () => { toast.style.display = "none"; openEvent(e); };
    toast.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.style.display = "none", 15000);
    if ("Notification" in window && Notification.permission === "granted") {
      const n = new Notification(`Person on ${e.camera}`, { body: when(e), icon: fileUrl(e, e.snapshot), tag: e.camera });
      n.onclick = () => { window.focus(); openEvent(e); };
    }
  }

  const btn = document.getElementById("notify-btn");
  if ("Notification" in window && window.isSecureContext && Notification.permission === "default") {
    btn.hidden = false;
    btn.onclick = () => Notification.requestPermission().then(() => btn.hidden = true);
  }

  const time = e => new Date(e.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const day = e => {
    const d = new Date(e.start), today = new Date();
    return d.toDateString() === today.toDateString() ? "" : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " ";
  };

  function card(e) {
    const el = document.createElement("article");
    el.className = "event" + (e.status === "active" ? " active" : "");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = fileUrl(e, e.snapshot);
    const title = document.createElement("div");
    title.textContent = `${day(e)}${time(e)} · ${duration(e)}`;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${e.people} person(s) · ${Math.round(e.score * 100)}% · ` +
      ({ active: "in progress", saving: "saving clip…", interrupted: "interrupted" }[e.status] || (e.clip ? "▶ clip" : "snapshot"));
    el.title = `${e.camera} · ${when(e)}`;
    el.append(img, title, meta);
    el.onclick = () => openEvent(e);
    return el;
  }

  function render(events) {
    for (const [cam, { strip }] of Object.entries(tiles)) {
      const mine = events.filter(e => e.camera === cam);
      if (mine.length) {
        strip.replaceChildren(...mine.map(card));
      } else {
        const none = document.createElement("p");
        none.className = "none";
        none.textContent = "No person events yet";
        strip.replaceChildren(none);
      }
    }
  }

  let known = null, lastJson = "";
  async function poll() {
    try {
      const text = await (await fetch("/api/events", { cache: "no-store" })).text();
      if (text !== lastJson) {
        lastJson = text;
        const events = JSON.parse(text);
        const active = new Set(events.filter(e => e.status === "active").map(e => e.camera));
        for (const [cam, { fig }] of Object.entries(tiles)) fig.classList.toggle("alert", active.has(cam));
        const key = e => e.camera + "/" + e.id;
        if (known) for (const e of events) if (!known.has(key(e)) && e.status === "active") alertEvent(e);
        known = new Set(events.map(key));
        render(events);
      }
    } catch (err) { /* server restarting; retry */ }
    setTimeout(poll, 3000);
  }
  poll();
</script>
</body>
</html>
"""

TILE = """<section class="cam" data-cam="{name}">
<figure><img src="/stream/{url}" data-src="/stream/{url}" alt="{name}"><figcaption>{name}</figcaption></figure>
<div class="strip"><p class="none">No person events yet</p></div>
</section>"""


def make_handler(streams):
    page = PAGE.replace("__TILES__", "\n".join(
        TILE.format(name=html.escape(n, quote=True), url=quote(n, safe="")) for n in streams
    )).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            parts = [unquote(p) for p in self.path.split("?", 1)[0].split("/")]
            path = "/".join(parts)
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path.startswith("/stream/") and path[8:] in streams:
                self._stream(streams[path[8:]])
            elif path.startswith("/snapshot/") and path.endswith(".jpg") \
                    and path[10:-4] in streams:
                self._snapshot(streams[path[10:-4]])
            elif path == "/api/events":
                body = json.dumps(recent_events(streams)).encode()
                self._send(200, "application/json", body)
            elif len(parts) == 4 and parts[1] == "events" and parts[2] in streams \
                    and EVENT_FILE_RE.match(parts[3]):
                self._file(EVENTS_DIR / parts[2] / parts[3])
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path):
            """Serve an event file, with single-range support so clips can seek."""
            try:
                f = open(path, "rb")
            except OSError:
                self._send(404, "text/plain", b"not found\n")
                return
            with f:
                size = path.stat().st_size
                start, end, status = 0, size - 1, 200
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
                if m and (m[1] or m[2]):
                    if m[1]:
                        start, end = int(m[1]), int(m[2]) if m[2] else size - 1
                    else:
                        start = max(size - int(m[2]), 0)
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", CONTENT_TYPES[path.suffix[1:]])
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                f.seek(start)
                remaining = end - start + 1
                try:
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

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
    pid_file = write_pid_file("stream_server")

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
