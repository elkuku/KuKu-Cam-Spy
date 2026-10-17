"""Shared helpers for the Python camera tools (stream_server.py, person_watch.py)."""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CAMERAS_CONFIG", ROOT_DIR / "cameras.json"))
RUN_DIR = ROOT_DIR / "run"
EVENTS_DIR = ROOT_DIR / "events"


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


def opt(cam, config, key, default):
    """Per-camera setting, falling back to the top-level one, then default."""
    if key in cam and cam[key] is not None:
        return cam[key]
    if key in config and config[key] is not None:
        return config[key]
    return default


def resolve_secret(env_var, what):
    value = os.environ.get(env_var)
    if not value:
        sys.exit(f"environment variable '{env_var}' is not set (required for {what})")
    return value


def resolve_password(cam):
    # Same rules as resolve_password() in lib/common.sh.
    env_var = cam.get("password_env")
    if env_var:
        return resolve_secret(env_var, f"camera {cam['name']}")
    password = cam.get("password")
    if not password:
        sys.exit(f"camera {cam['name']} has no password or password_env set")
    return password


def build_rtsp_url(cam, path=None):
    host = cam.get("host")
    if not host:
        sys.exit(f"camera {cam['name']} is missing 'host'")
    port = cam.get("port", 554)
    path = path or cam.get("path") or "/"
    user = quote(cam.get("username", ""), safe="")
    password = quote(resolve_password(cam), safe="")
    return f"rtsp://{user}:{password}@{host}:{port}{path}"


def rtsp_input_args(cam, config, path=None):
    return [
        "-rtsp_transport", config.get("rtsp_transport", "tcp"),
        "-timeout", "15000000",
        "-i", build_rtsp_url(cam, path),
    ]


def iter_jpeg_frames(pipe):
    """Yield complete JPEG images from an ffmpeg `-f mjpeg -` stdout pipe.

    ffmpeg's MJPEG output has no embedded thumbnails, and 0xFF bytes in
    entropy-coded data are stuffed, so SOI/EOI markers reliably delimit frames.
    """
    buf = b""
    while True:
        chunk = pipe.read1(65536)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2)
            if start < 0 or end < 0:
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]


def write_pid_file(name):
    RUN_DIR.mkdir(exist_ok=True)
    pid_file = RUN_DIR / f"{name}.pid"
    pid_file.write_text(str(os.getpid()))
    return pid_file


def write_json_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
