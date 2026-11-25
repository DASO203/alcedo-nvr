"""Alarm review by a local vision language model (Ollama or OpenAI-compatible).

The model gets 1-4 downscaled frames of the alarm plus your instructions and
must answer with JSON. Nothing leaves your network unless you point the URL
somewhere else.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import logging
import re
import time
from typing import Any

import aiohttp

from .config import LlmConfig

_LOGGER = logging.getLogger(__name__)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "alarm": {"type": "boolean"},
        "confidence": {"type": "number"},
        "objects": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
    },
    "required": ["alarm", "confidence", "objects", "description"],
}

SYSTEM_PROMPT = {
    "de": (
        "Du prüfst Alarme einer Überwachungskamera. Du bekommst {n} Bild(er) "
        "der Kamera '{camera}', ausgelöst durch '{trigger}'. Regeln des Nutzers: {rules}\n"
        'Antworte NUR mit JSON: {{"alarm": true|false, "confidence": 0.0-1.0, '
        '"objects": [erkannte Objekte], "description": "ein kurzer Satz auf Deutsch"}}. '
        "alarm=true nur, wenn die Regeln einen Alarm verlangen."
    ),
    "en": (
        "You review alarms of a security camera. You get {n} image(s) of camera "
        "'{camera}', triggered by '{trigger}'. User rules: {rules}\n"
        'Answer ONLY with JSON: {{"alarm": true|false, "confidence": 0.0-1.0, '
        '"objects": [detected objects], "description": "one short sentence"}}. '
        "alarm=true only if the rules require an alarm."
    ),
}


@dataclass(slots=True)
class Verdict:
    """Result of the review."""

    alarm: bool
    confidence: float
    objects: list[str]
    description: str
    model: str
    seconds: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_verdict(text: str) -> dict[str, Any]:
    """Extract and validate the JSON object from a model answer."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON in answer")
    data = json.loads(match.group(0))
    alarm = data.get("alarm")
    if isinstance(alarm, str):
        alarm = alarm.strip().lower() in ("true", "yes", "ja", "1")
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    objects = data.get("objects") or []
    if not isinstance(objects, list):
        objects = [str(objects)]
    return {
        "alarm": bool(alarm),
        "confidence": max(0.0, min(1.0, confidence)),
        "objects": [str(o)[:40] for o in objects][:10],
        "description": str(data.get("description", ""))[:300],
    }


class LlmReviewer:
    """Calls the configured local model."""

    def __init__(self, session: aiohttp.ClientSession, cfg: LlmConfig) -> None:
        self._session = session
        self._cfg = cfg
        self.last_error: str | None = None
        self.reviews = 0

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and bool(self._cfg.url and self._cfg.model)

    def _prompt(self, camera: str, trigger: str, n: int) -> str:
        template = SYSTEM_PROMPT.get(self._cfg.language, SYSTEM_PROMPT["en"])
        return template.format(n=n, camera=camera, trigger=trigger, rules=self._cfg.prompt)

    async def review(self, camera: str, trigger: str, images: list[bytes]) -> Verdict:
        """Review an alarm. Never raises - errors become a verdict with error set."""
        started = time.monotonic()
        prompt = self._prompt(camera, trigger, len(images))
        b64 = [base64.b64encode(img).decode() for img in images]
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout)
        try:
            if self._cfg.provider == "ollama":
                payload = {
                    "model": self._cfg.model,
                    "stream": False,
                    "format": VERDICT_SCHEMA,
                    "options": {"temperature": 0},
                    "messages": [{"role": "user", "content": prompt, "images": b64}],
                }
                async with self._session.post(
                    f"{self._cfg.url}/api/chat", json=payload, timeout=timeout
                ) as resp:
                    resp.raise_for_status()
                    text = (await resp.json())["message"]["content"]
            else:
                content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                content += [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
                    for b in b64
                ]
                payload = {
                    "model": self._cfg.model,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": content}],
                }
                async with self._session.post(
                    f"{self._cfg.url}/v1/chat/completions", json=payload, timeout=timeout
                ) as resp:
                    resp.raise_for_status()
                    text = (await resp.json())["choices"][0]["message"]["content"]
            data = parse_verdict(text)
            self.last_error = None
            self.reviews += 1
            return Verdict(
                model=self._cfg.model, seconds=round(time.monotonic() - started, 1), **data
            )
        except Exception as err:
            self.last_error = f"{type(err).__name__}: {err}"[:200]
            _LOGGER.warning("LLM review failed (%s) - alarm is kept", self.last_error)
            return Verdict(
                alarm=True,
                confidence=0.0,
                objects=[],
                description="",
                model=self._cfg.model,
                seconds=round(time.monotonic() - started, 1),
                error=self.last_error,
            )

    def is_confirmed(self, verdict: Verdict) -> bool:
        """An alarm counts as confirmed if the model says so with enough confidence.

        If the model failed, the alarm is treated as confirmed (fail-safe:
        better one false alarm than a missed burglary).
        """
        if verdict.error:
            return True
        return verdict.alarm and verdict.confidence >= self._cfg.min_confidence
