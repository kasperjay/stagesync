"""Setlist.fm API v2 Client.

Queries the Setlist.fm API with rate limiting (<= 1.8 req/sec) to correlate
video date and coordinates with live concert setlists, lineups, and venues.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from dateutil import parser as dt_parser
from pydantic import BaseModel, Field
import requests

from stagesync.config import get_settings

logger = logging.getLogger(__name__)

SETLIST_FM_API_BASE = "https://api.setlist.fm/rest/1.0"


class SetlistSong(BaseModel):
    """Song entry within a live setlist."""

    name: str
    info: Optional[str] = None
    is_cover: bool = False
    cover_artist: Optional[str] = None
    with_artist: Optional[str] = None
    encore: int = 0
    track_number: int = 1


class MatchedConcert(BaseModel):
    """Normalized concert match resolved from Setlist.fm."""

    setlist_fm_event_id: str
    headliner_artist: str
    lineup_artists: List[str] = Field(default_factory=list)
    tour_name: Optional[str] = None
    event_date: str  # YYYY-MM-DD
    venue_name: str
    venue_id: Optional[str] = None
    city: str
    state_province: Optional[str] = None
    country_code: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    setlist_songs: List[SetlistSong] = Field(default_factory=list)
    raw_setlist_data: Dict[str, Any] = Field(default_factory=dict)
    source_url: Optional[str] = None

    @property
    def song_titles(self) -> List[str]:
        """Flat list of all track names played in order."""
        return [s.name for s in self.setlist_songs if s.name]


class SetlistFmClient:
    """Client for Setlist.fm REST API v2 with rate-limiting token bucket."""

    _last_call_time: float = 0.0
    _lock = threading.Lock()

    def __init__(self, api_key: Optional[str] = None, timeout: float = 6.0) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.setlist_fm_api_key
        self.timeout = timeout

    @classmethod
    def _enforce_rate_limit(cls) -> None:
        """Enforce rate limit of <= 1.8 requests per second."""
        with cls._lock:
            elapsed = time.time() - cls._last_call_time
            if elapsed < 0.6:  # ~1.67 req/sec max
                time.sleep(0.6 - elapsed)
            cls._last_call_time = time.time()

    def _format_date_for_api(self, date_val: str) -> str:
        """Convert YYYY-MM-DD or ISO string into Setlist.fm DD-MM-YYYY format."""
        dt = dt_parser.parse(date_val)
        return dt.strftime("%d-%m-%Y")

    def _format_date_standard(self, date_val: str) -> str:
        """Convert any date string into standard ISO YYYY-MM-DD."""
        dt = dt_parser.parse(date_val)
        return dt.strftime("%Y-%m-%d")

    def search_by_geo_and_date(
        self,
        event_date: str,
        latitude: float,
        longitude: float,
        radius_km: int = 5,
    ) -> Optional[MatchedConcert]:
        """Search setlists matching exact event date and geographic radius."""
        if not self.api_key:
            logger.info("SETLIST_FM_API_KEY not configured, skipping Setlist.fm geo query.")
            return None

        date_param = self._format_date_for_api(event_date)
        params = {
            "date": date_param,
            "lat": f"{latitude:.4f}",
            "long": f"{longitude:.4f}",
            "radius": str(radius_km),
        }
        return self._query_setlists(params, fallback_date=event_date)

    def search_by_city_and_date(
        self,
        event_date: str,
        city_name: str,
        artist_name: Optional[str] = None,
    ) -> Optional[MatchedConcert]:
        """Search setlists by city name and event date with optional artist filter."""
        if not self.api_key:
            return None

        date_param = self._format_date_for_api(event_date)
        params: Dict[str, str] = {
            "date": date_param,
            "cityName": city_name,
        }
        if artist_name:
            params["artistName"] = artist_name

        return self._query_setlists(params, fallback_date=event_date)

    def _query_setlists(
        self,
        params: Dict[str, str],
        fallback_date: str,
    ) -> Optional[MatchedConcert]:
        """Execute HTTP request against Setlist.fm search endpoint."""
        self._enforce_rate_limit()

        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "StageSync/1.0",
        }
        url = f"{SETLIST_FM_API_BASE}/search/setlists"

        try:
            res = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            if res.status_code == 404:
                logger.debug("No setlists found for params: %s", params)
                return None
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            logger.warning("Setlist.fm request failed: %s", e)
            return None

        setlists = data.get("setlist", [])
        if not setlists:
            return None

        # Return best match (first setlist with populated tracks preferred)
        best = setlists[0]
        for s in setlists:
            sets = s.get("sets", {}).get("set", [])
            if sets:
                best = s
                break

        return self.parse_setlist_payload(best, fallback_date)

    def parse_setlist_payload(
        self,
        item: Dict[str, Any],
        fallback_date: str,
    ) -> MatchedConcert:
        """Parse raw Setlist.fm JSON object into MatchedConcert."""
        event_id = item.get("id", "")
        raw_date = item.get("eventDate", "")
        try:
            standard_date = self._format_date_standard(raw_date) if raw_date else fallback_date
        except Exception:
            standard_date = fallback_date

        artist_obj = item.get("artist", {})
        headliner = artist_obj.get("name", "Unknown Artist")

        tour_obj = item.get("tour", {})
        tour_name = tour_obj.get("name") if tour_obj else None

        venue_obj = item.get("venue", {})
        venue_name = venue_obj.get("name", "Unknown Venue")
        venue_id = venue_obj.get("id")

        city_obj = venue_obj.get("city", {})
        city_name = city_obj.get("name", "Unknown City")
        state_province = city_obj.get("state")
        country_code = city_obj.get("country", {}).get("code", "US")

        coords_obj = venue_obj.get("city", {}).get("coords", {})
        lat = coords_obj.get("lat")
        lon = coords_obj.get("long")

        # Parse songs array
        raw_sets = item.get("sets", {}).get("set", [])
        setlist_songs: List[SetlistSong] = []
        track_counter = 1

        for s_idx, set_group in enumerate(raw_sets):
            encore_num = set_group.get("encore", 0)
            song_list = set_group.get("song", [])
            for s in song_list:
                s_name = s.get("name", "").strip()
                if not s_name:
                    continue
                info = s.get("info")
                cover = s.get("cover")
                is_cover = bool(cover)
                cover_artist = cover.get("name") if cover else None
                with_obj = s.get("with")
                with_artist = with_obj.get("name") if with_obj else None

                setlist_songs.append(
                    SetlistSong(
                        name=s_name,
                        info=info,
                        is_cover=is_cover,
                        cover_artist=cover_artist,
                        with_artist=with_artist,
                        encore=encore_num,
                        track_number=track_counter,
                    )
                )
                track_counter += 1

        source_url = item.get("url")

        return MatchedConcert(
            setlist_fm_event_id=event_id,
            headliner_artist=headliner,
            lineup_artists=[headliner],
            tour_name=tour_name,
            event_date=standard_date,
            venue_name=venue_name,
            venue_id=venue_id,
            city=city_name,
            state_province=state_province,
            country_code=country_code,
            latitude=float(lat) if lat is not None else None,
            longitude=float(lon) if lon is not None else None,
            setlist_songs=setlist_songs,
            raw_setlist_data=item,
            source_url=source_url,
        )
