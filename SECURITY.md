# Security

Report vulnerabilities privately via **Security → Report a vulnerability**. Never post passwords, API keys or camera URLs with credentials.

| Topic | Implementation |
|---|---|
| API (port 8765) | Random 256-bit key in `/data/api_key`, required as `Authorization: Bearer` (or `token` query for camera hooks), compared in constant time |
| Web UI | Only via Home Assistant Ingress (requests from the Supervisor proxy only) |
| Secrets in logs | Camera / FTP passwords and credentials in URLs are redacted from every log line |
| File access | Clips / snapshots are only served from inside `storage_path` |
| Reolink | HTTPS with the camera's self-signed certificate (not verifiable) – keep cameras in an isolated LAN/VLAN |
| SFTP | Host key pinning (`remote_host_key`) or trust on first use, stored in `/data/sftp_known_hosts` |
| FTP | Plain FTP is unencrypted – only use it inside your LAN; prefer SFTP or FTPS |
| Network | Connections only to your cameras, your AI server and your export targets – no telemetry |
