"""Hybrid Setlist-Constrained AI Audio Matcher.

Leverages Gemini 2.5 Flash audio reasoning to disambiguate live concert performances
against the finite list of songs from that specific night's Setlist.fm roster.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field
import requests

from stagesync.config import get_settings

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class AIAudioJudgeResult(BaseModel):
    """Result of Setlist-constrained AI audio identification."""

    matched: bool = False
    song_title: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    setlist_index: Optional[int] = None
    lyrics_detected: Optional[str] = None
    reasoning: Optional[str] = None
    service: str = "gemini_multimodal"
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class AIAudioJudge:
    """Evaluates noisy live audio against a concert's setlist using Gemini Flash."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model if model is not None else getattr(settings, "gemini_model", "gemini-2.5-flash")

    def evaluate_live_clip(
        self,
        audio_path: Union[str, Path],
        candidate_songs: List[str],
        artist_name: str,
        venue_name: str = "Live Venue",
        event_date: str = "",
    ) -> AIAudioJudgeResult:
        """Identify which setlist track matches the 30-second audio recording."""
        path = Path(audio_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")

        if not self.api_key:
            logger.info("GEMINI_API_KEY not configured; using heuristic setlist fallback.")
            return self._heuristic_fallback(candidate_songs, reason="AI judge key not configured; setlist available for manual selection.")

        if not candidate_songs and not artist_name:
            logger.info("Neither setlist songs nor artist name provided; skipping AI audio judge.")
            return AIAudioJudgeResult(matched=False, confidence=0.0, reasoning="No artist or candidate songs provided.")

        # Build prompt listing the candidate setlist songs or artist catalog instruction
        if candidate_songs:
            setlist_listing = "\n".join(
                f"{i + 1}. {song}" for i, song in enumerate(candidate_songs)
            )
            setlist_context = (
                f"Their exact setlist for this concert was:\n{setlist_listing}\n\n"
                f"Identify which of the setlist songs (or encore/tape intro) is being performed."
            )
        else:
            setlist_context = (
                f"No pre-compiled setlist is available for this show. "
                f"Analyze {artist_name}'s catalog/discography and musical signature to identify which song is being performed."
            )

        prompt_text = (
            f"You are an expert musicologist and live concert sound engineer.\n"
            f"Attached is a 30-second live audio recording taken at {venue_name} on {event_date}.\n"
            f"The artist performing is {artist_name}.\n"
            f"{setlist_context}\n\n"
            f"Listen to the audio snippet (vocals, guitar riffs, drum beats, lyrics sung, bassline).\n"
            f"Identify which song is being performed.\n"
            f"Output JSON format strictly conforming to:\n"
            f'{{\n  "song_title": "string",\n  "confidence": 0.0 to 1.0,\n  "setlist_index": 1,\n  "lyrics_detected": "string",\n  "reasoning": "string"\n}}'
        )

        try:
            # Read and base64-encode audio file
            audio_bytes = path.read_bytes()
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

            mime_type = "audio/mp3" if path.suffix.lower() == ".mp3" else "audio/wav"

            url = f"{GEMINI_API_BASE}/{self.model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt_text},
                            {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": audio_b64,
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                },
            }

            res = requests.post(url, json=payload, timeout=60.0)
            res.raise_for_status()
            data = res.json()

            candidates = data.get("candidates", [])
            if candidates:
                text_part = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                return self._parse_gemini_json(text_part, candidate_songs, data)

        except Exception as e:
            logger.warning("Gemini AI audio judge request failed: %s", e)
            return self._heuristic_fallback(candidate_songs, reason=f"Gemini API request error: {e}")

        return self._heuristic_fallback(candidate_songs)

    def _parse_gemini_json(
        self,
        json_str: str,
        candidate_songs: List[str],
        raw_payload: Dict[str, Any],
    ) -> AIAudioJudgeResult:
        """Parse structured JSON from model response."""
        try:
            # Clean markdown codeblocks if present
            cleaned = json_str.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())

            title = data.get("song_title", "").strip()
            confidence = float(data.get("confidence", 0.0))
            setlist_index = data.get("setlist_index")
            lyrics = data.get("lyrics_detected")
            reasoning = data.get("reasoning")

            # Check if title matches one of the candidate songs
            matched = False
            if candidate_songs:
                for idx, c in enumerate(candidate_songs):
                    if c.lower() == title.lower() or c.lower() in title.lower():
                        matched = True
                        if setlist_index is None:
                            setlist_index = idx + 1
                        break

            # If no setlist candidate matched (or setlist was empty), allow high confidence identification
            if not matched and confidence >= 0.70 and title and title.lower() not in ("unidentifiable", "unknown", "none"):
                matched = True

            return AIAudioJudgeResult(
                matched=matched,
                song_title=title if matched else None,
                confidence=confidence if matched else 0.0,
                setlist_index=setlist_index,
                lyrics_detected=lyrics,
                reasoning=reasoning,
                service="gemini_multimodal",
                raw_payload=raw_payload,
            )
        except Exception as e:
            logger.warning("Failed to parse Gemini output: %s", e)
            return self._heuristic_fallback(candidate_songs, reason=f"Failed to parse Gemini output: {e}")

    def _heuristic_fallback(self, candidate_songs: List[str], reason: Optional[str] = None) -> AIAudioJudgeResult:
        """Fallback when Gemini is unavailable or not configured."""
        if not candidate_songs:
            return AIAudioJudgeResult(
                matched=False,
                confidence=0.0,
                reasoning=reason or "No candidates available for identification.",
            )
        # Without model, cannot reliably pick from candidates without acoustic signature
        default_reason = (
            "AI judge key not configured; setlist available for manual selection."
            if not self.api_key
            else "Audio could not be disambiguated against setlist."
        )
        return AIAudioJudgeResult(
            matched=False,
            confidence=0.0,
            reasoning=reason or default_reason,
        )
