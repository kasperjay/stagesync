"""Shazamio Acoustic Fingerprint Matcher.

Identifies live musical recordings via acoustic waveform fingerprinting.
Provides both async and synchronous execution with graceful failure handling.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AcousticMatchResult(BaseModel):
    """Normalized response from acoustic fingerprint recognition."""

    matched: bool = False
    song_title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    fingerprint_service: str = "shazamio"
    unavailable: bool = False
    error: Optional[str] = None
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class ShazamMatcher:
    """Wrapper around Shazamio acoustic fingerprinting service."""

    def __init__(self) -> None:
        self._has_shazamio = False
        try:
            import shazamio  # noqa: F401
            self._has_shazamio = True
        except ImportError:
            logger.info("shazamio package not installed; will use acoustic fallback.")

    async def recognize_async(self, audio_path: Union[str, Path]) -> AcousticMatchResult:
        """Asynchronously recognize audio track using Shazamio."""
        path = Path(audio_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Audio file for Shazam recognition not found: {path}")

        if not self._has_shazamio:
            logger.debug("shazamio not available; returning unmatched result.")
            return AcousticMatchResult(
                matched=False,
                confidence=0.0,
                unavailable=True,
                error="Shazamio package is not installed in the runtime environment.",
            )

        try:
            from shazamio import Shazam

            shazam = Shazam()
            recognize_fn = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song", None)
            if recognize_fn is None:
                raise AttributeError("Shazam client has neither 'recognize' nor 'recognize_song'")
            out = await recognize_fn(str(path))
            return self.parse_shazam_payload(out)
        except Exception as e:
            logger.warning("Shazam recognition failed on %s: %s", path, e)
            return AcousticMatchResult(
                matched=False,
                confidence=0.0,
                error=str(e),
                raw_payload={"error": str(e)},
            )

    def recognize(self, audio_path: Union[str, Path]) -> AcousticMatchResult:
        """Synchronous wrapper for acoustic recognition."""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # Inside an already running async loop
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(lambda: asyncio.run(self.recognize_async(audio_path))).result()
            return asyncio.run(self.recognize_async(audio_path))
        except Exception:
            return asyncio.run(self.recognize_async(audio_path))

    def recognize_many(
        self,
        audio_paths: List[Union[str, Path]],
        max_workers: int = 4,
    ) -> List[AcousticMatchResult]:
        """Recognize multiple windows concurrently while preserving input order."""
        if not audio_paths:
            return []
        worker_count = min(max(1, max_workers), len(audio_paths))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(executor.map(self.recognize, audio_paths))

    def parse_shazam_payload(self, payload: Dict[str, Any]) -> AcousticMatchResult:
        """Parse raw Shazamio response dictionary."""
        track = payload.get("track")
        if not track:
            return AcousticMatchResult(matched=False, confidence=0.0, raw_payload=payload)

        title = track.get("title")
        artist = track.get("subtitle")

        # Extract album if present in metadata sections
        album = None
        sections = track.get("sections", [])
        for sec in sections:
            if sec.get("type") == "SONG":
                for meta in sec.get("metadata", []):
                    if meta.get("title", "").lower() == "album":
                        album = meta.get("text")
                        break

        # Shazam matches on acoustic waveform have high confidence if returned
        confidence = 0.94 if (title and artist) else 0.50

        return AcousticMatchResult(
            matched=True,
            song_title=title,
            artist=artist,
            album=album,
            confidence=confidence,
            fingerprint_service="shazamio",
            raw_payload=payload,
        )
