#!/usr/bin/env python3
"""Poll TP-Link Tapo cameras' own motion/person detector and log/react to events.

Uses the camera's local control API via pytapo (unofficial, reverse-engineered
protocol - may break on firmware updates). Requires the camera's TP-Link
*cloud account* password (not the RTSP Camera Account) for cameras on the
newer SPAKE2+/TPAP firmware - see README.md.

Usage:
    ./motion_watch.py                 # watch all enabled cameras
    ./motion_watch.py front_door      # watch one camera
    ./motion_watch.py cam1 cam2       # watch a specific set

Stop with Ctrl-C, or `./stop.sh` / `./stop.sh motion_watch` (it writes a
run/motion_watch.pid file like the other scripts).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from pytapo import Tapo
except ImportError:
    sys.exit(
        "pytapo is not installed. Run:\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "then re-run with .venv/bin/python3 motion_watch.py"
    )

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CAMERAS_CONFIG", ROOT_DIR / "cameras.json"))
RUN_DIR = ROOT_DIR / "run"
MIN_POLL_SECONDS = 2  # cameras reject polls spaced < ~1.5s with error -40109

stop_event = threading.Event()


def log(cam_name, msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{cam_name}] {msg}", flush=True)


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f"config file not found: {CONFIG_FILE}")
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


def resolve_cloud_password(cam, config):
    env_var = cam.get("tapo_cloud_password_env") or config.get(
        "tapo_cloud_password_env", "TAPO_CLOUD_PASSWORD"
    )
    password = os.environ.get(env_var)
    if not password:
        sys.exit(
            f"environment variable '{env_var}' is not set "
            f"(required for camera '{cam['name']}')"
        )
    return password


def trigger_snapshot(cam_name):
    try:
        subprocess.Popen(
            [str(ROOT_DIR / "snapshot.sh"), cam_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log(cam_name, f"failed to launch snapshot.sh: {e}")


def watch_camera(cam, config):
    name = cam["name"]
    host = cam["host"]
    poll_seconds = max(
        cam.get("motion_poll_seconds", config.get("motion_poll_seconds", 5)),
        MIN_POLL_SECONDS,
    )
    snapshot_on_motion = cam.get("snapshot_on_motion", False)
    password = resolve_cloud_password(cam, config)

    last_signature = None
    backoff = 5

    while not stop_event.is_set():
        try:
            log(name, f"connecting to {host} (poll every {poll_seconds}s)")
            tapo = Tapo(host, "admin", password, cloudPassword=password)
            backoff = 5
            log(name, "connected, watching for motion/person detection")

            while not stop_event.is_set():
                alarm = tapo.getAlarm()
                signature = json.dumps(alarm, sort_keys=True)
                if last_signature is not None and signature != last_signature:
                    log(name, f"detection event: {alarm}")
                    if snapshot_on_motion:
                        trigger_snapshot(name)
                last_signature = signature
                stop_event.wait(poll_seconds)

        except Exception as e:
            log(name, f"error: {e} - reconnecting in {backoff}s")
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    log(name, "stopped")


def main():
    config = load_config()
    filters = sys.argv[1:] or ["all"]
    cameras = select_cameras(config, filters)
    if not cameras:
        sys.exit(f"no matching enabled camera(s) found for: {' '.join(filters)}")

    RUN_DIR.mkdir(exist_ok=True)
    pid_file = RUN_DIR / "motion_watch.pid"
    pid_file.write_text(str(os.getpid()))

    def handle_signal(signum, frame):
        log("main", "stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = []
    for cam in cameras:
        t = threading.Thread(target=watch_camera, args=(cam, config), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
