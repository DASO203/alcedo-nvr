# Changelog

## 0.3.0 - 2026-11-25

- **Cameras from Home Assistant**: new API `GET/POST /api/cameras`, `DELETE /api/cameras/{id}`. The integration takes over cameras from the Reolink integration, Generic camera, ring-mqtt and other RTSP cameras without entering them again. Stored in `/data/cameras.json`; the cameras restart internally when the list changes.
- Camera ids transliterate umlauts ("Haustür" → `haustuer`)

## 0.2.0 - 2026-11-25

- **Timelapse** of all events in a period (24 h, week, ...): clips sped up with captions (camera, time, AI description), cached per event, rendered with low CPU priority
- Timeline API for arbitrary ranges (up to 31 days), used by the new dashboard card of the integration
- **Ring**: the live stream is only opened during an event (opening it interrupts Ring's motion detection and drains batteries); otherwise the last image is shown
- Fixed: timelapse captions containing "%"

## 0.1.0 - 2026-11-25

- First release: recording without re-encoding (events / continuous / on demand / off)
- Reolink AI & motion polling, INSTAR alarm server hook, own motion detection, generic webhook
- Ring cameras via RTSP (ring-mqtt) with on-demand recording
- Local AI alarm review (Ollama / OpenAI-compatible)
- Export: local folder, SFTP, FTPS, FTP
- Retention by days and disk usage
- Web UI with events and timeline of all cameras
- Supervisor discovery for the Alcedo NVR integration
