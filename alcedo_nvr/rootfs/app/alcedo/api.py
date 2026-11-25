"""HTTP API (port 8765, token protected) and Ingress UI (port 8099, HA login)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hmac
import json
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from .sources import instar_label

if TYPE_CHECKING:
    from .__main__ import AlcedoApp

_LOGGER = logging.getLogger(__name__)
INGRESS_PROXY = "172.30.32.2"  # Supervisor ingress proxy
WEB_DIR = Path(__file__).parent / "web"
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _token_ok(request: web.Request, key: str) -> bool:
    supplied = ""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        supplied = header[7:]
    supplied = supplied or request.query.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied, key)


class Api:
    def __init__(self, app: AlcedoApp) -> None:
        self.app = app
        self._sockets: set[web.WebSocketResponse] = set()
        self._pending: set[asyncio.Task] = set()
        self._runners: list[web.AppRunner] = []

    # --------------------------------------------------------------- push

    def publish(self, message: dict[str, Any]) -> None:
        data = json.dumps(message)
        for ws in list(self._sockets):
            if not ws.closed:
                task = asyncio.create_task(ws.send_str(data))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

    # --------------------------------------------------------------- servers

    async def start(self) -> None:
        api = web.Application(middlewares=[self._token_middleware])
        self._routes(api, "/api")
        api.router.add_route("*", "/api/hook/instar/{camera}", self.hook_instar)
        api.router.add_route("*", "/api/hook/{camera}", self.hook_generic)

        ui = web.Application(middlewares=[self._ingress_middleware])
        ui.router.add_get("/", self.index)
        ui.router.add_get("/ui/key", self.api_key)
        self._routes(ui, "/ui")

        for application, port in ((api, 8765), (ui, 8099)):
            runner = web.AppRunner(application, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()  # noqa: S104 - container
            self._runners.append(runner)
        _LOGGER.info("API on :8765, UI (Ingress) on :8099")

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        for runner in self._runners:
            await runner.cleanup()

    def _routes(self, application: web.Application, prefix: str) -> None:
        r = application.router
        r.add_get(f"{prefix}/info", self.info)
        r.add_get(f"{prefix}/events", self.events)
        r.add_get(f"{prefix}/events/{{event_id}}", self.event)
        r.add_get(f"{prefix}/events/{{event_id}}/snapshot.jpg", self.event_snapshot)
        r.add_get(f"{prefix}/events/{{event_id}}/clip.mp4", self.event_clip)
        r.add_get(f"{prefix}/cameras/{{camera}}/snapshot.jpg", self.camera_snapshot)
        r.add_post(f"{prefix}/cameras/{{camera}}/detection", self.camera_detection)
        r.add_get(f"{prefix}/timeline", self.timeline)
        r.add_post(f"{prefix}/timelapse", self.timelapse_start)
        r.add_get(f"{prefix}/timelapse/{{job}}", self.timelapse_status)
        r.add_get(f"{prefix}/timelapse/{{job}}/video.mp4", self.timelapse_video)
        r.add_get(f"{prefix}/cameras/{{camera}}/recording.mp4", self.recording)
        r.add_get(f"{prefix}/cameras", self.cameras_list)
        if prefix == "/api":
            r.add_post(f"{prefix}/cameras", self.cameras_add)
            r.add_delete(f"{prefix}/cameras/{{camera}}", self.camera_remove)
            r.add_get(f"{prefix}/cameras/{{camera}}/stream", self.camera_stream)
            r.add_get(f"{prefix}/ws", self.websocket)

    @web.middleware
    async def _token_middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if not _token_ok(request, self.app.api_key):
            _LOGGER.debug("Rejected %s from %s (token)", request.path, request.remote)
            raise web.HTTPUnauthorized()
        return await handler(request)

    @web.middleware
    async def _ingress_middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        if request.remote != INGRESS_PROXY:
            raise web.HTTPForbidden()
        return await handler(request)

    # --------------------------------------------------------------- handlers

    def _camera(self, request: web.Request):
        cam = self.app.cameras.get(request.match_info["camera"])
        if cam is None:
            raise web.HTTPNotFound()
        return cam

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html")

    async def api_key(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"api_key": self.app.api_key, "port": 8765, "host": self.app.hostname}
        )

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response(await self.app.info())

    async def events(self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(500, int(request.query.get("limit", 50))))
        except ValueError:
            raise web.HTTPBadRequest() from None
        camera = request.query.get("camera")
        return web.json_response(await self.app.store.recent(limit, camera))

    async def _event(self, request: web.Request) -> dict[str, Any]:
        event = await self.app.store.get(request.match_info["event_id"])
        if event is None:
            raise web.HTTPNotFound()
        return event

    async def event(self, request: web.Request) -> web.Response:
        return web.json_response(await self._event(request))

    def _file(self, path: str | None, content_type: str) -> web.StreamResponse:
        if not path:
            raise web.HTTPNotFound()
        file = Path(path).resolve()
        storage = Path(self.app.cfg.storage_path).resolve()
        if storage not in file.parents or not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file, headers={"Content-Type": content_type})

    async def event_snapshot(self, request: web.Request) -> web.StreamResponse:
        return self._file((await self._event(request)).get("snapshot_path"), "image/jpeg")

    async def event_clip(self, request: web.Request) -> web.StreamResponse:
        return self._file((await self._event(request)).get("clip_path"), "video/mp4")

    async def camera_snapshot(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        try:
            data = await self.app.snapshot(cam)
        except Exception as err:
            raise web.HTTPServiceUnavailable(text=type(err).__name__) from err
        return web.Response(body=data, content_type="image/jpeg")

    async def timeline(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            start = float(q["start"]) if "start" in q else None
            end = float(q["end"]) if "end" in q else None
            return web.json_response(await self.app.timeline(q.get("day"), start, end))
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err

    async def timelapse_start(self, request: web.Request) -> web.Response:
        """Body: {hours | start+end, cameras?, only_confirmed?, mode?, speed?}."""
        try:
            body = await request.json()
            now = time.time()
            if "start" in body:
                start, end = float(body["start"]), float(body.get("end", now))
            else:
                hours = max(1.0, min(24 * 31, float(body.get("hours", 24))))
                # round to 5 minutes so repeated requests hit the cache
                end = now - now % 300
                start = end - hours * 3600
            cameras = body.get("cameras") or None
            job = await self.app.timelapse.request(
                start,
                end,
                cameras,
                bool(body.get("only_confirmed", True)),
                str(body.get("mode", "clips")),
                int(body.get("speed", 4)),
            )
        except (ValueError, TypeError) as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response(job)

    async def timelapse_status(self, request: web.Request) -> web.Response:
        job = self.app.timelapse.jobs.get(request.match_info["job"])
        if job is None:
            raise web.HTTPNotFound()
        return web.json_response(job)

    async def timelapse_video(self, request: web.Request) -> web.StreamResponse:
        job = self.app.timelapse.jobs.get(request.match_info["job"])
        if job is None or job["state"] != "done":
            raise web.HTTPNotFound()
        return self._file(job["path"], "video/mp4")

    async def recording(self, request: web.Request) -> web.StreamResponse:
        cam = self._camera(request)
        try:
            start = float(request.query["start"])
            duration = int(request.query.get("duration", 60))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest() from None
        try:
            clip = await self.app.recording_clip(cam, start, duration)
        except FileNotFoundError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        return web.FileResponse(clip, headers={"Content-Type": "video/mp4"})

    async def camera_stream(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        return web.json_response({"main": cam.main_url, "sub": cam.sub_url})

    async def cameras_list(self, request: web.Request) -> web.Response:
        return web.json_response([c.public_dict() for c in self.app.cfg.cameras])

    async def cameras_add(self, request: web.Request) -> web.Response:
        """Body: {"cameras": [{name, type, host, ..., ha_entity}]} (from Home Assistant).

        The cameras are stored in /data/cameras.json and the camera workers are
        restarted a second after the reply.
        """
        try:
            body = await request.json()
            raws = body.get("cameras") if isinstance(body, dict) else None
            if not isinstance(raws, list) or not raws:
                raise ValueError("cameras: list required")
            taken = {c.name for c in self.app.cfg.cameras if c.source == "config"}
            added = await asyncio.to_thread(self.app.managed.add, raws, taken)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        names = [c["name"] for c in added]
        if added:
            _LOGGER.info("Cameras added from Home Assistant: %s", ", ".join(names))
            self.app.request_reload()
        return web.json_response({"added": names, "restarting": bool(added)})

    async def camera_remove(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        if cam.source != "ha":
            raise web.HTTPConflict(
                text="This camera is configured in the app options; remove it there."
            )
        await asyncio.to_thread(self.app.managed.remove, cam.name)
        _LOGGER.info("Camera %s removed", cam.name)
        self.app.request_reload()
        return web.json_response({"removed": cam.id, "restarting": True})

    async def camera_detection(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        if enabled:
            self.app.events.paused.discard(cam.id)
        else:
            self.app.events.paused.add(cam.id)
        _LOGGER.info("Detection for %s %s", cam.name, "enabled" if enabled else "paused")
        self.publish({"type": "camera_update", "camera": cam.id, "detection": enabled})
        return web.json_response({"camera": cam.id, "detection": enabled})

    async def hook_instar(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        label = instar_label(request.query.get("active"))
        _LOGGER.debug("Instar alarm hook %s active=%s", cam.name, request.query.get("active"))
        if label in cam.objects or label in ("motion", "alarm", "alarm_input", "audio"):
            await self.app.events.trigger(cam.id, "camera", label)
        return web.Response(text="OK")

    async def hook_generic(self, request: web.Request) -> web.Response:
        cam = self._camera(request)
        label = request.query.get("label", "motion")[:32]
        await self.app.events.trigger(cam.id, "webhook", label)
        return web.Response(text="OK")

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        await ws.send_str(json.dumps({"type": "hello", "info": await self.app.info()}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._sockets.discard(ws)
        return ws
