"""Small MusicBrainz client for manual song-selection assistance."""

from __future__ import annotations

import logging
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2"


class MusicBrainzClient:
    """Search recording titles without adding a third-party dependency."""

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout

    def search_recordings(
        self,
        artist_name: str,
        query: str,
        limit: int = 25,
        raise_on_error: bool = False,
    ) -> List[Dict[str, str]]:
        artist = artist_name.strip()
        title_query = query.strip()
        if not artist or len(title_query) < 2:
            return []

        escaped_artist = artist.replace('"', '\\"')
        escaped_title = title_query.replace('"', '\\"')
        search = f'artist:"{escaped_artist}" AND recording:"{escaped_title}"'
        try:
            response = requests.get(
                f"{MUSICBRAINZ_API_BASE}/recording",
                params={"query": search, "fmt": "json", "limit": min(max(limit, 1), 100)},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "StageSync/1.0 (https://github.com/kasperjay/stagesync)",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            recordings = response.json().get("recordings", [])
        except Exception as exc:
            logger.warning("MusicBrainz recording search failed for %s: %s", artist, exc)
            if raise_on_error:
                raise RuntimeError(f"MusicBrainz discography search failed: {exc}") from exc
            return []

        results: List[Dict[str, str]] = []
        seen = set()
        for recording in recordings:
            title = (recording.get("title") or "").strip()
            if not title or title.casefold() in seen:
                continue
            seen.add(title.casefold())
            results.append({
                "title": title,
                "artist": artist,
                "source": "musicbrainz",
                "id": recording.get("id", ""),
            })
        return results
