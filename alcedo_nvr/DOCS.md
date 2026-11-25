# Alcedo NVR

Lean local NVR for **Reolink**, **INSTAR**, **Ring (via RTSP)** and any **RTSP** camera, with optional **local AI review** of every alarm by a vision language model. Runs as an app next to Home Assistant on the same machine – no extra VM.

🇩🇪 Deutsche Anleitung: [README.de.md](https://github.com/DASO203/alcedo-nvr/blob/main/README.de.md)

## How it works

1. **Recording** – the main stream is stored 1:1 (`ffmpeg -c copy`, no re-encoding) in short segments below `storage_path/segments/<camera>`.
   - `events`: only a short ring buffer is kept; on an alarm a clip with pre- and post-roll is cut from it.
   - `continuous`: segments are kept for `retention_days`.
   - `on_demand`: the stream is only opened while an alarm is active (Ring / battery cameras; no pre-roll).
   - `off`: snapshots only.
2. **Alarm sources**
   - Reolink: the camera's own AI (person / vehicle / animal) and motion, polled once per second.
   - INSTAR: the camera's *alarm server* calls Alcedo (see below).
   - Own motion detection on the sub stream (3 fps, 160×90 px – negligible CPU).
   - Any Home Assistant sensor via the action `alcedo_nvr.trigger` (e.g. Ring motion / doorbell).
3. **Local AI review** (optional) – 1–4 downscaled frames go to your local model (Ollama or an OpenAI-compatible local server) together with your rules in plain language. The model answers `alarm`, `confidence`, `objects` and a short `description`. If the model fails or times out, the alarm counts as confirmed (fail-safe).
4. **Export** of confirmed alarms (or all): local folder and/or SFTP / FTPS / FTP.
5. **Home Assistant integration** ([alcedo-nvr-integration](https://github.com/DASO203/alcedo-nvr-integration)): cameras, alarm sensors with the AI description, media browser, and automatic upload to pCloud via [pCloud Sync](https://github.com/DASO203/pcloud-sync-unofficial).

The web UI (sidebar → *Alcedo NVR*) shows cameras, events with AI verdicts and a **timeline of all cameras**: recording bars and alarm markers per camera for one day; a click plays the recording from that moment.

## Cameras from Home Assistant (no second setup)

Cameras that already exist in Home Assistant do not have to be entered here again. The **Alcedo NVR integration** finds them and asks once per camera:

- **Reolink integration**: host, login and channel are taken over, so Alcedo uses the camera's own AI.
- **Generic camera / ONVIF / other RTSP cameras**: the stream URL that Home Assistant uses. INSTAR (`/11`) and ring-mqtt (`:8554/…_live`) are recognised automatically.
- **ring-mqtt**: the *Stream Source* of the camera info sensor.

Confirm under *Settings → Devices & services → Discovered*, or choose *Alcedo NVR → Configure → Take over cameras from Home Assistant*. Taken-over cameras are stored in `/data/cameras.json` (readable by the app only, part of the app backup) and appear in the log as `source=ha`. When cameras are added or removed, the app restarts its cameras internally for a few seconds. Remove them via *Configure → Remove taken-over cameras*.

You can mix both ways: cameras in the options below **and** cameras taken over from Home Assistant.

## Camera configuration

```yaml
cameras:
  - name: Einfahrt
    type: reolink
    host: 192.168.1.20
    username: admin
    password: "!secret-in-the-ui"
    objects: person, vehicle          # what triggers an alarm
    record: events
  - name: Garten
    type: instar
    host: 192.168.1.21
    username: admin
    password: "..."
    detect: both                      # camera alarm server + own motion detection
    objects: person, motion
  - name: Haustür Ring
    type: ring
    rtsp_main: rtsp://03cabcc9-ring-mqtt:8554/0123456789ab_live
  - name: Carport
    type: generic
    rtsp_main: rtsp://user:pass@192.168.1.30:554/stream1
    rtsp_sub: rtsp://user:pass@192.168.1.30:554/stream2
    motion_sensitivity: 60
```

| Field | Default | Notes |
|---|---|---|
| `type` | – | `reolink`, `instar`, `ring`, `generic` |
| `host`, `username`, `password` | – | Reolink / INSTAR. Stream URLs are built automatically. |
| `channel` | 0 | Reolink NVR / Home Hub channel (0 = first camera) |
| `use_https` | Reolink: true | Reolink uses a self-signed certificate (not verified – keep cameras in your LAN). |
| `rtsp_main`, `rtsp_sub` | built | Override stream URLs (required for `ring` and `generic`). Some 4K Reolink models need `…/h265Preview_01_main`. |
| `snapshot_url` | built | Custom JPEG URL. Fallback is a frame from the stream. |
| `record` | `events` (`ring`: `on_demand`) | `events`, `continuous`, `on_demand`, `off` |
| `detect` | `camera` (`generic`: `motion`, `ring`: `off`) | `camera`, `motion`, `both`, `off` |
| `objects` | `person, vehicle` | `person`, `vehicle`, `animal`, `motion` |
| `motion_sensitivity` | 50 | 1–100 (own motion detection) |
| `enabled` | true | |

### Reolink
Enable HTTP**S** (or HTTP + `use_https: false`) and RTSP in the camera (*Settings → Network → Advanced → Server settings*). Use a separate user with *guest* rights if possible.

### INSTAR alarm server
Camera web UI → *Alarm → Alarm Server*:
- Server: IP of your Home Assistant, port `8765`
- Path: `/api/hook/instar/<camera id>` (camera id = name in lower case, spaces → `_`, e.g. `garten`)
- Query parameter `token` = API key (shown in the Alcedo web UI under *Connection for the integration*)
- Enable *append alarm trigger to query* so Alcedo knows the source (`active=…`).

### Ring
Ring has no local RTSP. Use the **ring-mqtt** app, which provides an RTSP stream per camera (`rtsp://<ring-mqtt host>:8554/<device id>_live`, exact value in the *Stream Source* attribute of its camera *info* sensor; add `user:password@` if you set livestream credentials).

Ring behaviour according to the ring-mqtt documentation: continuous streaming is not recommended, a running live stream **interrupts Ring's own motion detection**, and Ring ends live streams after roughly 10 minutes. Alcedo therefore
- records Ring cameras with `record: on_demand` - the stream is only opened while an event is active,
- never opens the stream just for a preview image (the last event image is shown; set `snapshot_url` if you have a still image URL),
- limits events to 5 minutes.

Trigger alarms from Home Assistant:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.haustuer_motion   # from ring-mqtt / Ring integration
    to: "on"
actions:
  - action: alcedo_nvr.trigger
    data:
      entity_id: camera.haustuer_ring
      label: person
```

## Local AI (vision LLM)

Install an Ollama server (e.g. an Ollama app from a community app repository, or Ollama on another machine in your LAN) and pull a **small vision model**, e.g. `qwen2.5vl:3b`, `gemma3:4b` or `moondream`. Then:

```yaml
llm_enabled: true
llm_provider: ollama
llm_url: http://172.30.32.1:11434   # host of the Ollama app as seen from Alcedo
llm_model: qwen2.5vl:3b
llm_language: de
llm_prompt: "Alarm only for people or unknown vehicles. Ignore cats, birds, shadows, rain and headlights."
```

On an Intel N100 class mini PC expect roughly tens of seconds per review on CPU (estimate – depends on model, frames and RAM). The alarm itself is raised immediately; the AI verdict follows. 16 GB RAM are recommended when an LLM runs next to Home Assistant.

## Timeline & timelapse

- Web UI -> *Timeline*: one row per camera for a day, recording bars and alarm markers, click to play.
- Dashboard: the integration ships the card `custom:alcedo-timeline-card` (24 h / 3 days / 7 days, click to play, **Timelapse** button).
- Timelapse: every event of the period becomes a short part - its clip sped up (default 4x, max. 40 s per clip) or its snapshot as a 1.5 s still - captioned *camera / time / AI description*. Parts are cached and rendered at 960x540 with low CPU priority; finished timelapses are kept for 2 days in `storage_path/timelapse`.
- The action `alcedo_nvr.create_timelapse` (integration) returns the file path, e.g. to upload a weekly timelapse to pCloud.

## Storage & export

| Option | Default | |
|---|---|---|
| `storage_path` | `/media/alcedo` | Use a large disk or NAS share mounted as *media* in Home Assistant (Settings → System → Storage). Exclude it from backups. |
| `retention_days` | 7 | events and continuous recordings |
| `max_storage_percent` | 85 | above: oldest recordings first, then oldest events are deleted |
| `export_only_confirmed` | true | |
| `export_local` / `export_local_path` | true / `/media/alcedo/export` | hand-over folder (e.g. pCloud Sync watch folder) |
| `export_remote` | false | `remote_protocol`: `sftp` (recommended), `ftps`, `ftp` (unencrypted) |
| `remote_host_key` | – | pin the SFTP host key; empty = trust on first use (stored in `/data/sftp_known_hosts`) |

## Security
- API (port 8765) only with the random API key (`/data/api_key`); the web UI only through Home Assistant Ingress.
- Camera and FTP passwords are removed from all log lines.
- Cameras taken over from Home Assistant are validated (only `rtsp://`/`rtsps://` streams and `http(s)://` snapshot URLs) and stored with file mode 600.
- Clips are only served from inside `storage_path`.
- The app talks only to your cameras, your AI server and your export targets. No cloud, no telemetry.
