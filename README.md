# kuku-cam-spy

Shell scripts to record and snapshot RTSP network cameras using
username/password authentication, driven by `ffmpeg`/`ffprobe` and a JSON
config file. Only use this against cameras you own or are authorized to
access.

## Requirements

- `ffmpeg` / `ffprobe`
- `jq`
- bash

## Setup

1. Copy the example config and edit it:

   ```sh
   cp cameras.json.example cameras.json
   chmod 600 cameras.json
   ```

2. Fill in each camera's `host`, `port`, `username`, `path`, and either:
   - `password`: the plaintext password directly in the file, or
   - `password_env`: the name of an environment variable holding the
     password (leave `password` as `null`). This keeps the real secret out
     of `cameras.json`, e.g.:

     ```sh
     export CAM_BACKYARD_PASSWORD='s3cret'
     ```

3. `path` is the RTSP stream path after `host:port`. It's usually the same
   across cameras of the same brand/model, e.g.:

   | Brand               | Typical path                      |
   |---------------------|------------------------------------|
   | Hikvision / Dahua    | `/Streaming/Channels/101`          |
   | Reolink              | `/h264Preview_01_main`             |
   | Axis                 | `/axis-media/media.amp`            |
   | TP-Link (Tapo)        | `/stream1` (HD) or `/stream2` (SD) |
   | Generic ONVIF        | check the camera's web UI/manual   |

   TP-Link (Tapo) cameras require a separate **Camera Account** username/
   password, set in the Tapo app under Advanced Settings → Camera Account —
   not your TP-Link cloud login.

   Use `ffprobe` to verify a URL before relying on it:

   ```sh
   ffprobe -rtsp_transport tcp "rtsp://user:pass@192.168.1.50:554/Streaming/Channels/101"
   ```

## Usage

```sh
./record.sh                  # record all enabled cameras (foreground)
./record.sh front_door       # record just one camera
./stop.sh                    # stop all running recorders
./stop.sh front_door         # stop one camera's recorder

./snapshot.sh                # one JPEG snapshot per enabled camera
./snapshot.sh front_door     # snapshot just one camera

./cleanup.sh                 # delete files older than retention_days
```

- `record.sh` writes segmented MP4 files (default 15 min each, no
  re-encoding) to `recordings/<camera_name>/`, and auto-restarts a camera's
  stream if the connection drops.
- `snapshot.sh` writes a single JPEG to `snapshots/<camera_name>/`.
- Config knobs (top-level in `cameras.json`): `segment_seconds`,
  `rtsp_transport` (`tcp` is recommended over `udp` for reliability),
  `retention_days`.

## Running continuously (systemd)

```ini
# /etc/systemd/system/cam-record.service
[Unit]
Description=RTSP camera recorder
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/kuku-cam-spy
ExecStart=/path/to/kuku-cam-spy/record.sh
ExecStop=/path/to/kuku-cam-spy/stop.sh
Restart=on-failure
User=yourusername

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now cam-record.service
```

## Periodic snapshots + cleanup (cron)

```cron
*/5 * * * * /path/to/kuku-cam-spy/snapshot.sh >> /path/to/kuku-cam-spy/snapshot.log 2>&1
0 3 * * *   /path/to/kuku-cam-spy/cleanup.sh   >> /path/to/kuku-cam-spy/cleanup.log 2>&1
```

## Live view in the browser

`stream_server.py` serves a web page showing all cameras live. ffmpeg
transcodes each RTSP feed to MJPEG, which any browser can display. It needs
nothing beyond Python 3 and ffmpeg (no pip packages). A camera's ffmpeg process
starts only while someone is watching it, and all viewers share it.

```sh
./stream_server.py               # serve all enabled cameras
./stream_server.py front_door    # serve just one camera
./stop.sh                        # stops this too (same run/*.pid convention)
```

Open <http://127.0.0.1:8080/> and click a tile to view it fullscreen. Other
endpoints:

- `/stream/<camera>`: raw MJPEG stream, usable in `<img src>`, VLC, etc.
- `/snapshot/<camera>.jpg`: the current frame as a single JPEG

Below the live tiles, the page lists the person events recorded by
`person_watch.py` (see next section).

Config knobs (top-level, and all but the first two can be overridden per camera):

| Key            | Default     | Meaning                                         |
|----------------|-------------|-------------------------------------------------|
| `stream_bind`  | `127.0.0.1` | Address to listen on; `0.0.0.0` exposes it on your LAN |
| `stream_port`  | `8080`      | HTTP port                                       |
| `stream_fps`   | `5`         | Frame rate sent to the browser                  |
| `stream_width` | `1280`      | Max width; frames are only downscaled, never enlarged |
| `stream_path`  | `path`      | Per camera: RTSP path for live view, e.g. Tapo `/stream2` (SD) to save CPU |

**The page has no authentication.** Anyone who can reach the port can watch
your cameras. Keep the default `127.0.0.1` bind and use an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 yourhost`). If you need wider access, put it
behind a reverse proxy with auth/TLS, and never forward the port from the
internet.

Browsers allow ~6 simultaneous HTTP/1.1 connections per host and each live
tile holds one open, so the grid page is practical for up to about 5 cameras.

## Person detection, clips and alerts

`person_watch.py` watches every enabled camera for **people**, whatever the
camera brand. It decodes a small feed (`detect_width` px wide, `detect_fps`
frames/s) and runs it through a YOLOX-s object detector (COCO model from the
[OpenCV model zoo](https://huggingface.co/opencv/object_detection_yolox),
Apache-2.0) on the CPU. Only the "person" class counts, so trees, shadows,
cars and animals don't trigger events. The model (36 MB) is downloaded to
`models/` on first start and its checksum is verified.

For each event it writes to `events/<camera>/`:

- `<id>.jpg`: snapshot with the detected people boxed
- `<id>.mp4`: clip from `clip_pre_seconds` before the first detection until
  the person has been gone for `event_end_seconds`, cut without re-encoding
  from a rolling buffer of the **main** stream (`run/buffer/<camera>/`)
- `<id>.json`: metadata, which the live-view page reads

It also sends a Telegram photo, plus the clip once it's saved, when
configured.

```sh
.venv/bin/pip install -r requirements.txt   # once: adds opencv + numpy
.venv/bin/python3 person_watch.py              # watch all enabled cameras
.venv/bin/python3 person_watch.py front_door   # watch just one camera
./stop.sh                                       # stops this too
```

**Web page notifications:** when `stream_server.py` runs, the live page lists
recent events (click one to play its clip). A camera tile gets a red outline
while a person is in view, and a pop-up appears for each new event. Click
"Enable desktop notifications" for OS notifications as well. Browsers only
allow these on `localhost`/HTTPS, so over a plain-HTTP LAN address you get
the in-page pop-up only.

**Telegram:**

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. `export TELEGRAM_BOT_TOKEN='123456:ABC...'`, then set `telegram_enabled`
   and `telegram_chat_id` in `cameras.json`.

Settings are top-level defaults, and each one can be overridden per camera:

| Key                      | Default              | Meaning |
|--------------------------|----------------------|---------|
| `person_detection`       | `true`               | Set `false` on a camera to skip it |
| `detect_path`            | `stream_path`/`path` | RTSP path analysed; use the substream |
| `detect_fps`             | `2`                  | Frames per second analysed |
| `detect_width`           | `640`                | Width of analysed frames and snapshots |
| `detect_min_score`       | `0.6`                | Confidence threshold (0–1); raise it if you get false alarms |
| `detect_min_frames`      | `2`                  | Consecutive frames with a person needed to start an event |
| `event_end_seconds`      | `10`                 | Event ends after no person for this long |
| `event_max_seconds`      | `120`                | Split events longer than this |
| `clip_enabled`           | `true`               | Keep a video clip per event |
| `clip_pre_seconds`       | `5`                  | Video kept from before the first detection |
| `telegram_enabled`       | `false`              | Send events to Telegram |
| `telegram_chat_id`       | –                    | Chat/user/group id to send to |
| `telegram_bot_token_env` | `TELEGRAM_BOT_TOKEN` | Env var holding the bot token |
| `telegram_send_clip`     | `true`               | Also send the clip (when under Telegram's 50 MB limit) |

Notes:

- **CPU:** one detection takes ~45 ms on a modern desktop CPU (more on a
  Raspberry Pi), so 2 fps × a handful of cameras is light. Lower
  `detect_fps` on slow machines.
- **RTSP connections:** each camera can get up to four connections at once:
  `record.sh` (main), the clip buffer (main), detection (sub) and live view
  (sub). Some cameras limit concurrent sessions, and when that limit is hit
  ffmpeg reports `Error opening input: Operation not permitted`. Some Tapo
  models, for example, accept only **one** client on `/stream1` but several
  on `/stream2`. So always set `stream_path` to the substream; then live view
  and detection share it, and `/stream1` is left for one consumer. That's
  either the clip buffer or `record.sh`, not both: if you run `record.sh` on
  such a camera, set `clip_enabled: false` for it.
- `cleanup.sh` deletes events older than `retention_days` too.

## Motion detection (TP-Link / Tapo cameras only)

> For most uses, prefer `person_watch.py` (above): it works on all cameras
> and only reacts to people. This older script stays for Tapo owners who
> want the camera's own detector.

`motion_watch.py` polls a Tapo camera's own built-in motion/person detector
(the camera's own AI/PIR result) via its local control API, instead of
analyzing the video ourselves. This is a different, unofficial protocol from
RTSP, implemented via the reverse-engineered [`pytapo`](https://github.com/JurajNyiri/pytapo)
library — it may break on firmware updates, and it's Python-only (the rest of
this toolkit is pure shell/ffmpeg).

### Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Newer Tapo firmware (SPAKE2+ / "TPAP" local protocol — confirmed on a C500
2.0) requires authenticating with username `"admin"` and your **TP-Link
cloud account password** — not the RTSP Camera Account. Set it as an
environment variable and reference it from `cameras.json`:

```sh
export TAPO_CLOUD_PASSWORD='your-cloud-account-password'
```

```json
{
  "tapo_cloud_password_env": "TAPO_CLOUD_PASSWORD",
  "motion_poll_seconds": 5,
  "cameras": [
    {
      "name": "front_door",
      "...": "...",
      "snapshot_on_motion": true
    }
  ]
}
```

`tapo_cloud_password_env` and `motion_poll_seconds` can be set per-camera to
override the top-level default. `snapshot_on_motion` (per camera, default
`false`) runs `snapshot.sh <camera>` whenever a new detection event fires.

### Usage

```sh
.venv/bin/python3 motion_watch.py              # watch all enabled cameras
.venv/bin/python3 motion_watch.py front_door   # watch just one camera
./stop.sh                                       # stops this too (same run/*.pid convention)
```

### Known limitations

- **No SD card → coarse signal.** The precise `searchDetectionList` event API
  (with type/timestamp/bounding info) requires a microSD card in the camera.
  Without one, `motion_watch.py` falls back to polling `getAlarm()`
  (`getLastAlarmInfo`), which has no SD-card requirement but only exposes the
  *last* alarm, not a clean event stream — the script detects a "new event"
  by diffing the raw response between polls. **The exact response fields
  weren't live-verified against a real camera** (testing would have required
  pasting a TP-Link cloud password into this session, which was skipped) —
  the first time you run it, check the logged raw payload under
  `detection event: {...}` and confirm it actually changes on real motion.
- **Rate limit:** don't poll faster than ~1.5s (`getAlarm`/`getEvents` can
  return error `-40109` if called too fast); the script enforces a 2s floor
  and defaults to 5s.
- Prefer a real microSD card in the camera if you want reliable, typed
  motion/person/vehicle/pet events via `searchDetectionList` instead of this
  crude fallback — that path isn't implemented here but is a straightforward
  extension of `motion_watch.py` using `tapo.getEvents()`.

## Security notes

- `cameras.json` is gitignored and should be `chmod 600`.
- Prefer `password_env` over embedding plaintext passwords in the config
  file, especially if the file might ever be committed, backed up, or
  shared.
- Credentials are URL-encoded automatically when building the RTSP URL, so
  special characters (`@`, `:`, `/`, etc.) in usernames/passwords are safe.
