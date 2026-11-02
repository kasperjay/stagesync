"""Hybrid Setlist-Constrained AI Audio Matcher.

Leverages Gemini 2.5 Flash audio reasoning to disambiguate live concert performances
against the finite list of songs from that specific night's Setlist.fm roster.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field
import requests

from stagesync.config import get_settings

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MAX_ATTEMPTS = 4
GEMINI_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class AIAudioJudgeResult(BaseModel):
    """Result of Setlist-constrained AI audio identification."""

    matched: bool = False
    song_title: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    setlist_index: Optional[int] = None
    lyrics_detected: Optional[str] = None
    reasoning: Optional[str] = None
    error: Optional[str] = None
    service: str = "gemini_multimodal"
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class AIAudioJudge:
    """Evaluates noisy live audio against a concert's setlist using Gemini Flash."""

    _PLACEHOLDER_TITLES = {
        "unidentifiable",
        "unknown",
        "none",
        "null",
        "n/a",
        "live performance",
        "unidentified",
        "unidentified song",
        "unidentified clip",
        "instrumental",
        "clip",
    }

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
        allow_open_catalog: bool = False,
    ) -> AIAudioJudgeResult:
        """Identify which setlist track matches the 30-second audio recording."""
        path = Path(audio_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")

        if not self.api_key:
            logger.info("GEMINI_API_KEY not configured; using heuristic setlist fallback.")
            return self._heuristic_fallback(
                candidate_songs,
                reason="AI judge key not configured; setlist available for manual selection.",
                error="GEMINI_API_KEY is not configured.",
            )

        usable_artist = (artist_name or "").strip()
        if usable_artist.casefold() in {"", "live artist", "unknown artist", "unknown"}:
            usable_artist = ""

        if not candidate_songs and not (allow_open_catalog and usable_artist):
            logger.info("No candidate songs provided (open catalog disabled or artist unknown); skipping AI audio judge.")
            return AIAudioJudgeResult(
                matched=False,
                confidence=0.0,
                reasoning="No setlist songs available for this concert.",
            )

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
            f"Identify which song is being performed. If the song cannot be confidently identified, set song_title to \"\" and confidence below 0.5 and explain why in reasoning.\n"
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

            res = self._post_with_retry(url, payload)
            data = res.json()

            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or [{}]
                text_part = parts[0].get("text", "") if parts else ""
                return self._parse_gemini_json(text_part, candidate_songs, data)

            return self._heuristic_fallback(
                candidate_songs,
                reason="Gemini returned an empty candidate response.",
            )

        except Exception as e:
            err_msg = self._safe_error_message(e)
            logger.warning("Gemini AI audio judge request failed: %s", err_msg)
            return self._heuristic_fallback(candidate_songs, reason=err_msg, error=err_msg)

    def match_lyrics_to_discography(
        self,
        lyrics: str,
        artist_name: str,
        discography: List[str],
    ) -> AIAudioJudgeResult:
        """Suggest a discography title from detected lyrics without confirming it."""
        usable_lyrics = (lyrics or "").strip()
        usable_artist = (artist_name or "").strip()
        candidates = [title.strip() for title in discography if title and title.strip()]
        if not usable_lyrics or not usable_artist or not candidates:
            return AIAudioJudgeResult(
                matched=False,
                reasoning="Lyrics, performing artist, or discography candidates are missing.",
                service="lyrics_discography",
            )
        if not self.api_key:
            return AIAudioJudgeResult(
                matched=False,
                reasoning="Gemini API key is not configured for lyrics-to-discography matching.",
                service="lyrics_discography",
            )

        prompt = (
            "You are matching a short, possibly imperfectly transcribed lyric fragment to an artist's discography.\n"
            f"Artist: {usable_artist}\n"
            f"Detected lyrics: {usable_lyrics}\n\n"
            "Choose only a title from the numbered discography below. Do not invent a title. "
            "Return an empty song_title and confidence below 0.5 when the evidence is insufficient.\n"
            + "\n".join(f"{index + 1}. {title}" for index, title in enumerate(candidates))
            + '\n\nReturn JSON: {"song_title":"string","confidence":0.0,"reasoning":"string"}'
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        try:
            url = f"{GEMINI_API_BASE}/{self.model}:generateContent?key={self.api_key}"
            response = self._post_with_retry(url, payload)
            data = response.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or [{}]
            text = parts[0].get("text", "") if parts else ""
            result = self._parse_gemini_json(text, candidates, data)
            normalized = self._normalize_title(result.song_title)
            valid_candidate = any(
                normalized == self._normalize_title(candidate)
                or normalized in self._normalize_title(candidate)
                or self._normalize_title(candidate) in normalized
                for candidate in candidates
            )
            if not valid_candidate:
                result.matched = False
                result.song_title = None
                result.confidence = 0.0
                result.reasoning = result.reasoning or "Lyrics did not map to a discography title."
            result.service = "lyrics_discography"
            result.setlist_index = None
            result.lyrics_detected = usable_lyrics
            return result
        except Exception as exc:
            error = self._safe_error_message(exc)
            logger.warning("Lyrics discography match failed: %s", error)
            return AIAudioJudgeResult(
                matched=False,
                lyrics_detected=usable_lyrics,
                reasoning=error,
                error=error,
                service="lyrics_discography",
            )

    @staticmethod
    def _normalize_title(value: Optional[str]) -> str:
        return "".join(char.casefold() for char in (value or "") if char.isalnum())

    def _post_with_retry(self, url: str, payload: Dict[str, Any]) -> requests.Response:
        """POST to Gemini with bounded retries for transient provider failures."""
        for attempt in range(GEMINI_MAX_ATTEMPTS):
            try:
                response = requests.post(url, json=payload, timeout=60.0)
                if response.status_code not in GEMINI_RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                    return response

                if attempt == GEMINI_MAX_ATTEMPTS - 1:
                    response.raise_for_status()

                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "Gemini audio request returned HTTP %s; retrying in %.2fs (attempt %s/%s)",
                    response.status_code,
                    delay,
                    attempt + 1,
                    GEMINI_MAX_ATTEMPTS,
                )
                time.sleep(delay)
            except requests.RequestException:
                raise

        raise RuntimeError("Gemini request retry loop exited unexpectedly")

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        """Return Retry-After when usable, otherwise exponential backoff with jitter."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(30.0, (2**attempt) + random.uniform(0.0, 0.25))

    @staticmethod
    def _safe_error_message(error: Exception) -> str:
        """Describe provider failures without exposing the API key in request URLs."""
        if isinstance(error, requests.HTTPError):
            response = error.response
            status = response.status_code if response is not None else None
            if status:
                return f"Gemini API returned HTTP {status} after retries."
            return "Gemini API request failed after retries."
        return f"Gemini API request error: {type(error).__name__}: {error}"

    def _parse_gemini_json(
        self,
        json_str: str,
        candidate_songs: List[str],
        raw_payload: Dict[str, Any],
    ) -> AIAudioJudgeResult:
        """Parse structured JSON from model response."""
        try:
            # Clean markdown codeblocks if present
            cleaned = (json_str or "").strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())

            raw_title = data.get("song_title")
            title = str(raw_title).strip() if raw_title is not None else ""
            raw_conf = data.get("confidence")
            confidence = max(0.0, min(1.0, float(raw_conf))) if raw_conf is not None else 0.0
            setlist_index = data.get("setlist_index")
            lyrics = data.get("lyrics_detected")
            reasoning = data.get("reasoning")

            is_placeholder = not title or title.casefold() in self._PLACEHOLDER_TITLES

            # Check if title matches one of the candidate songs
            matched = False
            if not is_placeholder and candidate_songs:
                for idx, c in enumerate(candidate_songs):
                    if c.lower() == title.lower() or c.lower() in title.lower():
                        matched = True
                        if setlist_index is None:
                            setlist_index = idx + 1
                        break

            # If no setlist candidate matched (or setlist was empty), allow high confidence identification
            if not matched and not is_placeholder and confidence >= 0.70:
                matched = True

            return AIAudioJudgeResult(
                matched=matched,
                song_title=title if matched else None,
                confidence=confidence if matched else 0.0,
                setlist_index=setlist_index if matched else None,
                lyrics_detected=lyrics,
                reasoning=reasoning or ("Audio could not be confidently matched to a song." if not matched else None),
                service="gemini_multimodal",
                raw_payload=raw_payload,
            )
        except Exception as e:
            logger.warning("Failed to parse Gemini output: %s", e)
            err_msg = f"Failed to parse Gemini output: {e}"
            return self._heuristic_fallback(candidate_songs, reason=err_msg, error=err_msg)

    def _heuristic_fallback(
        self,
        candidate_songs: List[str],
        reason: Optional[str] = None,
        error: Optional[str] = None,
    ) -> AIAudioJudgeResult:
        """Fallback when Gemini is unavailable or not configured."""
        if not candidate_songs:
            return AIAudioJudgeResult(
                matched=False,
                confidence=0.0,
                reasoning=reason or "No candidates available for identification.",
                error=error,
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
            error=error,
        )
