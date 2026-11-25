<div align="center">

<img src="alcedo_nvr/icon.png" width="96" alt="">

# Alcedo NVR

**Lean local NVR for Home Assistant – with a local AI that reviews every alarm.**

🇬🇧 English · [🇩🇪 Deutsch](README.de.md)

</div>

Alcedo (Latin for *kingfisher*) records **Reolink**, **INSTAR**, **Ring** (via RTSP) and any **RTSP** camera, lets a **local vision language model** decide whether an alarm is real, and exports confirmed alarms **locally**, via **SFTP/FTPS/FTP** or to **pCloud** through [pCloud Sync](https://github.com/DASO203/pcloud-sync-unofficial).

It runs as a Home Assistant **app** (formerly add-on) on the same machine – no extra VM.

| Repository | What | Install |
|---|---|---|
| **alcedo-nvr** (this) | the app: recording, detection, AI, export, web UI with timeline | Settings → Apps → add repository |
| [alcedo-nvr-integration](https://github.com/DASO203/alcedo-nvr-integration) | the integration: entities, events, media browser, pCloud hand-over | Marketplace |

## Highlights

- 📷 **Takes over cameras that already exist in Home Assistant** (Reolink integration, Generic camera / RTSP, ring-mqtt) – no second setup
- 🎥 Recording without re-encoding (`-c copy`): events with pre-roll, continuous, **on demand** (Ring / battery cameras) or off
- 🚨 Alarm sources: Reolink on-camera AI & motion, INSTAR alarm server, lightweight own motion detection, any HA sensor via `alcedo_nvr.trigger`
- 🧠 Local AI review with your rules in plain language (Ollama or OpenAI-compatible local servers)
- 🕒 **Timeline of all cameras** – in the app and as **dashboard card** (24 h / 3 d / 7 d), click to play
- ⏩ **Timelapse** of all alarms in a period with captions (camera, time, AI description)
- 📤 Export: local folder, SFTP (host key pinning / TOFU), FTPS, FTP
- 🧹 Retention by days **and** max. disk usage
- 🔐 Token-protected API, UI only via Home Assistant Ingress, passwords redacted from logs, no cloud, no telemetry

## Install

1. **Settings → Apps → App store → ⋮ → Repositories** → add `https://github.com/DASO203/alcedo-nvr`
2. Install **Alcedo NVR**, configure cameras, start
3. Install the [Alcedo NVR integration](https://github.com/DASO203/alcedo-nvr-integration) from the Marketplace – the app is discovered automatically

Full documentation: [alcedo_nvr/DOCS.md](alcedo_nvr/DOCS.md)

## License

MIT. Not affiliated with Reolink, INSTAR, Ring, Ollama or pCloud.
