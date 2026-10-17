#!/usr/bin/env python3
"""Detect people on the configured RTSP cameras and record event clips.

For every camera, a low-resolution feed is decoded at a few frames per second
and run through a YOLOX person detector (COCO model from the OpenCV model zoo,
CPU only). When a person is seen, an event is written to events/<camera>/:

    <id>.json   event metadata (read by stream_server.py's web page)
    <id>.jpg    snapshot with the detected people boxed
    <id>.mp4    clip from clip_pre_seconds before until the person is gone

Clips are cut, without re-encoding, from a short rolling buffer of the main
stream kept in run/buffer/<camera>/. Optionally a Telegram message is sent
for each event.

Usage:
    .venv/bin/python3 person_watch.py              # watch all enabled cameras
    .venv/bin/python3 person_watch.py front_door   # watch just one camera
    ./stop.sh                                      # stops this too
"""

import hashlib
import json
import mimetypes
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit(
        "opencv/numpy are not installed. Run:\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "then re-run with .venv/bin/python3 person_watch.py"
    )

from camlib import (
    EVENTS_DIR, ROOT_DIR, RUN_DIR, iter_jpeg_frames, load_config, log, opt,
    resolve_secret, rtsp_input_args, select_cameras, write_json_atomic,
    write_pid_file,
)

# YOLOX-s, Apache-2.0, https://huggingface.co/opencv/object_detection_yolox
MODEL_URL = (
    "https://huggingface.co/opencv/object_detection_yolox/resolve/main/"
    "object_detection_yolox_2022nov.onnx"
)
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_FILE = ROOT_DIR / "models" / "object_detection_yolox_2022nov.onnx"

BUFFER_SEGMENT_SECONDS = 2
RESTART_DELAY_SECONDS = 5
TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024  # Bot API upload limit

stop_event = threading.Event()
processes = set()
processes_lock = threading.Lock()


def run_ffmpeg(args, **kwargs):
    """Start ffmpeg and register it so shutdown can terminate it."""
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        stdin=subprocess.DEVNULL, **kwargs,
    )
    with processes_lock:
        processes.add(proc)
    return proc


def reap(proc):
    proc.wait()
    with processes_lock:
        processes.discard(proc)


def ensure_model():
    if MODEL_FILE.exists():
        return
    MODEL_FILE.parent.mkdir(exist_ok=True)
    log("main", f"downloading person detection model to {MODEL_FILE}")
    tmp = MODEL_FILE.with_suffix(".part")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        tmp.unlink()
        sys.exit(f"model checksum mismatch (got {digest}), refusing to use it")
    tmp.rename(MODEL_FILE)


class PersonDetector:
    """YOLOX-s COCO detector, reduced to the 'person' class.

    Decoding follows the OpenCV model zoo's yolox.py (Apache-2.0). One network
    is shared by all cameras; OpenCV already parallelizes a single inference.
    """

    INPUT = 640
    STRIDES = (8, 16, 32)

    def __init__(self, model_path):
        self.net = cv2.dnn.readNet(str(model_path))
        self.lock = threading.Lock()
        grids, strides = [], []
        for stride in self.STRIDES:
            size = self.INPUT // stride
            xv, yv = np.meshgrid(np.arange(size), np.arange(size))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def detect(self, img, min_score):
        """Return [(x0, y0, x1, y1, score), ...] in img pixel coordinates."""
        h, w = img.shape[:2]
        ratio = min(self.INPUT / h, self.INPUT / w)
        padded = np.full((self.INPUT, self.INPUT, 3), 114, np.float32)
        resized = cv2.resize(img, (int(w * ratio), int(h * ratio)))
        padded[:resized.shape[0], :resized.shape[1]] = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis]

        with self.lock:
            self.net.setInput(blob)
            dets = self.net.forward(self.net.getUnconnectedOutLayersNames())[0][0].copy()

        scores = dets[:, 4] * dets[:, 5]  # objectness * P(person)
        keep = scores >= min_score
        if not keep.any():
            return []
        dets, scores = dets[keep], scores[keep]
        centers = (dets[:, :2] + self.grids[keep]) * self.strides[keep]
        sizes = np.exp(dets[:, 2:4]) * self.strides[keep]
        boxes = np.concatenate([centers - sizes / 2, sizes], axis=1) / ratio  # x, y, w, h
        picked = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), min_score, 0.45)
        return [
            (int(x), int(y), int(x + bw), int(y + bh), float(scores[i]))
            for i in np.array(picked).flatten()
            for x, y, bw, bh in [boxes[i]]
        ]


class TelegramNotifier:
    """Sends Telegram messages from a single background worker."""

    def __init__(self):
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, token, chat_id, method, field, path, caption=None):
        self.jobs.put((token, chat_id, method, field, path, caption))

    def _worker(self):
        while True:
            token, chat_id, method, field, path, caption = self.jobs.get()
            try:
                self._post(token, method, {"chat_id": str(chat_id), "caption": caption or ""},
                           field, path)
            except Exception as e:
                log("telegram", f"{method} failed: {e}")

    @staticmethod
    def _post(token, method, fields, file_field, path):
        boundary = uuid.uuid4().hex
        body = b""
        for key, value in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\""
                     f"\r\n\r\n{value}\r\n").encode()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()


class SegmentBuffer:
    """Rolling buffer of short main-stream segments to cut event clips from."""

    def __init__(self, cam, config, keep_seconds):
        self.name = cam["name"]
        self.input_args = rtsp_input_args(cam, config)
        self.keep_seconds = keep_seconds
        self.dir = RUN_DIR / "buffer" / self.name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not stop_event.is_set():
            proc = run_ffmpeg([
                *self.input_args,
                "-map", "0:v", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                "-f", "segment", "-segment_time", str(BUFFER_SEGMENT_SECONDS),
                "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
                str(self.dir / "%s.ts"),
            ])
            while proc.poll() is None and not stop_event.wait(10):
                self._prune()
            reap(proc)
            if not stop_event.is_set():
                log(self.name, f"clip buffer ffmpeg exited, restarting in {RESTART_DELAY_SECONDS}s")
                stop_event.wait(RESTART_DELAY_SECONDS)

    def _segments(self):
        """Sorted [(start_epoch, path)], the last one possibly still being written."""
        segs = []
        for p in self.dir.glob("*.ts"):
            try:
                segs.append((int(p.stem), p))
            except ValueError:
                pass
        return sorted(segs)

    def _prune(self):
        cutoff = time.time() - self.keep_seconds
        segs = self._segments()
        for (_, path), (next_start, _) in zip(segs, segs[1:]):
            if next_start < cutoff:
                path.unlink(missing_ok=True)

    def build_clip(self, t_from, t_to, out_path):
        # Wait until the segment containing t_to has been closed.
        deadline = time.time() + 3 * BUFFER_SEGMENT_SECONDS + 10
        while time.time() < deadline and not stop_event.is_set():
            segs = self._segments()
            if segs and segs[-1][0] > t_to:
                break
            time.sleep(1)
        segs = self._segments()
        chosen = [
            path for (start, path), (next_start, _) in zip(segs, segs[1:])
            if next_start > t_from and start < t_to
        ]
        if not chosen:
            log(self.name, "no buffered video for this event, clip skipped")
            return False
        list_file = out_path.with_suffix(".txt")
        list_file.write_text("".join(f"file '{p}'\n" for p in chosen))
        tmp = out_path.with_suffix(".part")
        proc = run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
                           "-movflags", "+faststart", "-f", "mp4", "-y", str(tmp)])
        reap(proc)
        list_file.unlink(missing_ok=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            log(self.name, f"clip assembly failed (ffmpeg status {proc.returncode})")
            return False
        tmp.rename(out_path)
        return True


class CameraWatcher:
    def __init__(self, cam, config, detector, telegram):
        self.name = cam["name"]
        self.detector = detector
        self.fps = opt(cam, config, "detect_fps", 2)
        self.width = opt(cam, config, "detect_width", 640)
        self.min_score = opt(cam, config, "detect_min_score", 0.6)
        self.min_frames = opt(cam, config, "detect_min_frames", 2)
        self.end_seconds = opt(cam, config, "event_end_seconds", 10)
        self.max_seconds = opt(cam, config, "event_max_seconds", 120)
        self.clip_pre = opt(cam, config, "clip_pre_seconds", 5)
        # The detector looks at the light substream (stream_path) by default;
        # clips always come from the main stream (path).
        detect_path = cam.get("detect_path") or cam.get("stream_path") or cam.get("path")
        self.input_args = rtsp_input_args(cam, config, detect_path)

        self.buffer = None
        if opt(cam, config, "clip_enabled", True):
            keep = self.clip_pre + self.max_seconds + 60
            self.buffer = SegmentBuffer(cam, config, keep)

        self.telegram = None
        if opt(cam, config, "telegram_enabled", False):
            chat_id = opt(cam, config, "telegram_chat_id", None)
            if not chat_id:
                sys.exit(f"telegram_enabled is set but telegram_chat_id is missing (camera {self.name})")
            token_env = opt(cam, config, "telegram_bot_token_env", "TELEGRAM_BOT_TOKEN")
            self.telegram = (telegram, resolve_secret(token_env, f"Telegram on camera {self.name}"),
                             chat_id, opt(cam, config, "telegram_send_clip", True))

        self.dir = EVENTS_DIR / self.name
        self._mark_interrupted_events()
        self.cond = threading.Condition()
        self.latest = None  # (timestamp, jpeg)
        threading.Thread(target=self._capture, daemon=True).start()
        threading.Thread(target=self._detect_loop, daemon=True).start()

    def _capture(self):
        """Keep only the newest frame, so a slow detector never builds a backlog."""
        while not stop_event.is_set():
            log(self.name, f"connecting detection feed ({self.fps} fps)")
            proc = run_ffmpeg([
                *self.input_args, "-an",
                "-vf", f"fps={self.fps},scale='min({self.width},iw)':-2",
                "-q:v", "4", "-f", "mjpeg", "-",
            ], stdout=subprocess.PIPE)
            for jpeg in iter_jpeg_frames(proc.stdout):
                with self.cond:
                    self.latest = (time.time(), jpeg)
                    self.cond.notify_all()
            reap(proc)
            if not stop_event.is_set():
                log(self.name, f"detection feed lost, reconnecting in {RESTART_DELAY_SECONDS}s")
                stop_event.wait(RESTART_DELAY_SECONDS)

    def _detect_loop(self):
        hits = 0
        event = None
        while not stop_event.is_set():
            with self.cond:
                self.cond.wait_for(lambda: self.latest is not None, timeout=1)
                frame, self.latest = self.latest, None
            now = time.time()
            if frame is not None:
                ts, jpeg = frame
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                people = self.detector.detect(img, self.min_score) if img is not None else []
                if people:
                    if event:
                        event["last_seen"] = ts
                        event["score"] = max(event["score"], max(p[4] for p in people))
                    else:
                        hits += 1
                        if hits >= self.min_frames:
                            event = self._start_event(ts, img, people)
                elif not event:
                    hits = 0
            if event and (now - event["last_seen"] > self.end_seconds
                          or now - event["start_ts"] > self.max_seconds):
                self._end_event(event, now)
                event, hits = None, 0

    def _start_event(self, ts, img, people):
        self.dir.mkdir(parents=True, exist_ok=True)
        event_id = datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H-%M-%S")
        for x0, y0, x1, y1, score in people:
            cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 255), 2)
            cv2.putText(img, f"{score:.0%}", (x0, max(y0 - 6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        snapshot = self.dir / f"{event_id}.jpg"
        cv2.imwrite(str(snapshot), img)
        event = {
            "id": event_id,
            "camera": self.name,
            "start": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
            "end": None,
            "start_ts": ts,
            "last_seen": ts,
            "people": len(people),
            "score": max(p[4] for p in people),
            "snapshot": snapshot.name,
            "clip": None,
            "status": "active",
        }
        self._save(event)
        log(self.name, f"person detected ({len(people)}, score {event['score']:.2f}) -> event {event_id}")
        if self.telegram:
            notifier, token, chat_id, _ = self.telegram
            stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            notifier.send(token, chat_id, "sendPhoto", "photo", snapshot,
                          f"Person detected on {self.name} at {stamp}")
        return event

    def _end_event(self, event, now):
        event["end"] = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        event["status"] = "saving" if self.buffer else "done"
        self._save(event)
        log(self.name, f"event {event['id']} ended after {now - event['start_ts']:.0f}s")
        if self.buffer:
            threading.Thread(target=self._save_clip, args=(event, now), daemon=True).start()

    def _save_clip(self, event, t_to):
        clip = self.dir / f"{event['id']}.mp4"
        if self.buffer.build_clip(event["start_ts"] - self.clip_pre, t_to, clip):
            event["clip"] = clip.name
            log(self.name, f"saved clip {clip}")
            if self.telegram and self.telegram[3]:
                notifier, token, chat_id, _ = self.telegram
                if clip.stat().st_size <= TELEGRAM_MAX_UPLOAD:
                    notifier.send(token, chat_id, "sendVideo", "video", clip,
                                  f"Clip: {self.name} {event['id']}")
                else:
                    log(self.name, "clip larger than Telegram's 50 MB limit, not sent")
        event["status"] = "done"
        self._save(event)

    def _mark_interrupted_events(self):
        """Events left 'active'/'saving' by a previous run will never finish."""
        for path in self.dir.glob("*.json"):
            try:
                event = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if event.get("status") in ("active", "saving"):
                event["status"] = "interrupted"
                write_json_atomic(path, event)

    def _save(self, event):
        write_json_atomic(self.dir / f"{event['id']}.json", event)


def main():
    config = load_config()
    filters = sys.argv[1:] or ["all"]
    cameras = [c for c in select_cameras(config, filters)
               if opt(c, config, "person_detection", True)]
    if not cameras:
        sys.exit(f"no matching enabled camera(s) with person_detection for: {' '.join(filters)}")

    ensure_model()
    detector = PersonDetector(MODEL_FILE)
    telegram = TelegramNotifier()
    pid_file = write_pid_file("person_watch")

    def handle_signal(signum, frame):
        log("main", "stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for cam in cameras:
        CameraWatcher(cam, config, detector, telegram)
    log("main", f"watching for people on: {', '.join(c['name'] for c in cameras)}")

    stop_event.wait()
    with processes_lock:
        for proc in processes:
            proc.terminate()
    time.sleep(1)
    pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
