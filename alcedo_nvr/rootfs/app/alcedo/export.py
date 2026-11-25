"""Export of finished events: local folder and/or FTP / FTPS / SFTP push.

The local export folder is the hand-over point for the pCloud Sync
integration: add it as watch folder there (or let the Alcedo NVR integration
call pCloud Sync directly).
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import logging
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import ssl

from .config import ExportConfig

_LOGGER = logging.getLogger(__name__)
KNOWN_HOSTS = Path("/data/sftp_known_hosts")


def remote_dir(base: str, camera_id: str, day: str) -> str:
    """Remote target folder: <base>/<camera>/<YYYY-MM-DD>."""
    parts = [p for p in PurePosixPath(base).parts if p not in ("/", "..", ".")]
    return "/" + "/".join([*parts, camera_id, day])


class Exporter:
    """Copies event files to the configured targets."""

    def __init__(self, cfg: ExportConfig) -> None:
        self._cfg = cfg
        self.last_error: str | None = None

    async def export(self, camera_id: str, day: str, files: list[Path]) -> dict[str, str]:
        """Export files; returns target -> "ok" | error text. Never raises."""
        results: dict[str, str] = {}
        files = [f for f in files if f.is_file()]
        if not files:
            return results
        if self._cfg.local:
            try:
                target = Path(self._cfg.local_path) / camera_id / day
                await asyncio.to_thread(self._copy_local, files, target)
                results["local"] = "ok"
            except OSError as err:
                results["local"] = str(err)
        if self._cfg.remote and self._cfg.host:
            folder = remote_dir(self._cfg.path, camera_id, day)
            try:
                if self._cfg.protocol == "sftp":
                    await self._sftp(files, folder)
                else:
                    await asyncio.to_thread(self._ftp, files, folder)
                results[self._cfg.protocol] = "ok"
            except Exception as err:
                results[self._cfg.protocol] = f"{type(err).__name__}: {err}"[:200]
        errors = {k: v for k, v in results.items() if v != "ok"}
        self.last_error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
        if errors:
            _LOGGER.warning("Export of %s failed: %s", files[0].stem, self.last_error)
        else:
            _LOGGER.info("Exported %s to %s", files[0].stem, ", ".join(results))
        return results

    @staticmethod
    def _copy_local(files: list[Path], target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for file in files:
            part = target / (file.name + ".part")
            shutil.copy2(file, part)
            part.replace(target / file.name)  # atomic: watchers never see half files

    def _ftp(self, files: list[Path], folder: str) -> None:
        cfg = self._cfg
        if cfg.protocol == "ftps":
            ftp: ftplib.FTP = ftplib.FTP_TLS(  # noqa: S321 - user choice, encrypted
                context=ssl.create_default_context(), timeout=30
            )
        else:
            ftp = ftplib.FTP(timeout=30)  # noqa: S321 - plain FTP only if the user selects it
        with ftp:
            ftp.connect(cfg.host, cfg.port or 21)
            ftp.login(cfg.username, cfg.password)
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()  # encrypt the data channel too
            current = ""
            for part in folder.strip("/").split("/"):
                current = f"{current}/{part}"
                with contextlib.suppress(ftplib.error_perm):  # exists
                    ftp.mkd(current)
            for file in files:
                with file.open("rb") as handle:
                    ftp.storbinary(f"STOR {current}/{file.name}", handle)

    async def _sftp(self, files: list[Path], folder: str) -> None:
        import asyncssh

        cfg = self._cfg
        host_id = cfg.host if (cfg.port or 22) == 22 else f"[{cfg.host}]:{cfg.port}"
        if cfg.host_key:
            known: object = asyncssh.import_known_hosts(f"{host_id} {cfg.host_key.strip()}\n")
        elif KNOWN_HOSTS.exists():
            known = str(KNOWN_HOSTS)
        else:
            known = None  # trust on first use, see below
        async with asyncssh.connect(
            cfg.host,
            port=cfg.port or 22,
            username=cfg.username,
            password=cfg.password,
            known_hosts=known,
            connect_timeout=20,
        ) as conn:
            if known is None:
                key = conn.get_server_host_key()
                line = f"{host_id} {key.export_public_key().decode().strip()}\n"
                await asyncio.to_thread(KNOWN_HOSTS.write_text, line, "utf-8")
                _LOGGER.warning(
                    "SFTP: first connection to %s - host key %s stored (trust on first use). "
                    "Set remote_host_key to pin it explicitly.",
                    cfg.host,
                    key.get_fingerprint(),
                )
            async with conn.start_sftp_client() as sftp:
                await sftp.makedirs(folder, exist_ok=True)
                for file in files:
                    await sftp.put(str(file), posixpath.join(folder, file.name))
