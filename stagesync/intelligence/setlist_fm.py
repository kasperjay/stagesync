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

    def search_by_venue_and_date(
        self,
        event_date: str,
        venue_name: str,
        city_name: Optional[str] = None,
    ) -> Optional[MatchedConcert]:
        """Search setlists matching exact venue name and event date."""
        if not self.api_key or not venue_name:
            return None

        date_param = self._format_date_for_api(event_date)
        params: Dict[str, str] = {
            "date": date_param,
            "venueName": venue_name,
        }
        if city_name:
            params["cityName"] = city_name

        data = self._fetch_setlists_raw(params)
        if not data and city_name:
            # Retry without cityName constraint in case of administrative city mismatch
            params_retry = {"date": date_param, "venueName": venue_name}
            data = self._fetch_setlists_raw(params_retry)

        if not data:
            return None

        setlists = data.get("setlist", [])
        if not setlists:
            return None

        # Return best match (prioritize setlist with tracks)
        best = setlists[0]
        for s in setlists:
            sets = s.get("sets", {}).get("set", [])
            if sets:
                best = s
                break

        matched = self.parse_setlist_payload(best, event_date, all_setlists=setlists)
        return matched

    def search_by_city_and_date(
        self,
        event_date: str,
        city_name: str,
        artist_name: Optional[str] = None,
        venue_name_hint: Optional[str] = None,
        max_pages: int = 3,
    ) -> Optional[MatchedConcert]:
        """Search setlists by city name and event date with optional artist or venue filter."""
        if not self.api_key or not city_name:
            return None

        date_param = self._format_date_for_api(event_date)

        for page in range(1, max_pages + 1):
            params: Dict[str, str] = {
                "date": date_param,
                "cityName": city_name,
                "p": str(page),
            }
            if artist_name:
                params["artistName"] = artist_name

            data = self._fetch_setlists_raw(params)
            if not data:
                break
            setlists = data.get("setlist", [])
            if not setlists:
                break

            # If venue_name_hint is provided, match by venue name
            if venue_name_hint:
                hint_lower = venue_name_hint.lower().strip()
                venue_matches = []
                for s in setlists:
                    v_name = s.get("venue", {}).get("name", "").lower()
                    if hint_lower in v_name or v_name in hint_lower:
                        venue_matches.append(s)
                if venue_matches:
                    matched = self.parse_setlist_payload(
                        venue_matches[0], event_date, all_setlists=venue_matches
                    )
                    return matched

            # If artist_name was specified and matched
            if artist_name:
                return self.parse_setlist_payload(setlists[0], event_date, all_setlists=setlists)

            # If only 1 setlist exists for the whole city on that date and no venue hint
            if len(setlists) == 1 and not venue_name_hint:
                return self.parse_setlist_payload(setlists[0], event_date, all_setlists=setlists)

        return None

    def search_by_geo_and_date(
        self,
        event_date: str,
        latitude: float,
        longitude: float,
        radius_km: int = 5,
        city_hint: Optional[str] = None,
        venue_hint: Optional[str] = None,
    ) -> Optional[MatchedConcert]:
        """Setlist.fm API v2 search/setlists does not support lat/long params natively.
        Routes to search_by_venue_and_date or search_by_city_and_date with spatial constraints."""
        if venue_hint:
            res = self.search_by_venue_and_date(event_date=event_date, venue_name=venue_hint, city_name=city_hint)
            if res:
                return res
        if city_hint:
            return self.search_by_city_and_date(event_date=event_date, city_name=city_hint, venue_name_hint=venue_hint)
        return None

    def search_concerts(
        self,
        event_date: Optional[str] = None,
        artist_name: Optional[str] = None,
        venue_name: Optional[str] = None,
        city_name: Optional[str] = None,
        tour_name: Optional[str] = None,
        state: Optional[str] = None,
        country_code: Optional[str] = None,
        max_pages: int = 3,
        max_results: int = 30,
    ) -> List[MatchedConcert]:
        """Search Setlist.fm for selectable concert records using any filters."""
        if not self.api_key:
            return []

        filters = (event_date, artist_name, venue_name, city_name, tour_name, state, country_code)
        if not any(value and str(value).strip() for value in filters):
            return []

        params: Dict[str, str] = {}
        if event_date:
            params["date"] = self._format_date_for_api(event_date)
        for key, value in (
            ("artistName", artist_name),
            ("venueName", venue_name),
            ("cityName", city_name),
            ("tourName", tour_name),
            ("state", state),
        ):
            if value and value.strip():
                params[key] = value.strip()
        if country_code and country_code.strip():
            params["countryCode"] = country_code.strip().upper()

        results: List[MatchedConcert] = []
        raw_results: List[Dict[str, Any]] = []
        seen_ids = set()
        for page in range(1, max_pages + 1):
            data = self._fetch_setlists_raw({**params, "p": str(page)})
            raw_setlists = data.get("setlist", []) if data else []
            if not raw_setlists:
                break
            for raw_setlist in raw_setlists:
                event_id = raw_setlist.get("id")
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                raw_results.append(raw_setlist)
                if len(raw_results) >= max_results:
                    break
            if len(raw_results) >= max_results:
                break
            if len(raw_setlists) < 20:
                break

        grouped_setlists = self._group_setlists_by_show(raw_results)
        lineup_cache: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for raw_setlist in raw_results:
            show_setlists = grouped_setlists[self._show_key(raw_setlist)]
            if len(show_setlists) == 1 and artist_name:
                show_key = self._show_key(raw_setlist)
                if show_key not in lineup_cache:
                    lineup_cache[show_key] = self._fetch_show_lineup(raw_setlist)
                show_setlists = lineup_cache[show_key]

            result = self.parse_setlist_payload(
                raw_setlist,
                event_date or "",
                all_setlists=show_setlists,
            )
            results.append(result)

        return results

    @staticmethod
    def _show_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
        """Build a stable key for setlists belonging to the same show."""
        venue = item.get("venue", {}) or {}
        city = venue.get("city", {}) or {}
        event_date = item.get("eventDate", "")
        venue_id = venue.get("id") or venue.get("name", "")
        city_name = city.get("name", "")
        return (str(event_date), str(venue_id).casefold(), str(city_name).casefold())

    def _group_setlists_by_show(
        self,
        setlists: List[Dict[str, Any]],
    ) -> Dict[Tuple[str, str, str], List[Dict[str, Any]]]:
        grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for item in setlists:
            grouped.setdefault(self._show_key(item), []).append(item)
        return grouped

    def _fetch_show_lineup(
        self,
        item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Fetch all artist setlists for a selected show when artist filtering hid the bill."""
        venue = item.get("venue", {}) or {}
        city = venue.get("city", {}) or {}
        params: Dict[str, str] = {
            "date": str(item.get("eventDate", "")),
        }
        if venue.get("name"):
            params["venueName"] = venue["name"]
        if city.get("name"):
            params["cityName"] = city["name"]
        if city.get("state"):
            params["state"] = city["state"]
        country = (city.get("country") or {}).get("code")
        if country:
            params["countryCode"] = country
        lineup: List[Dict[str, Any]] = []
        for page in range(1, 4):
            data = self._fetch_setlists_raw({**params, "p": str(page)})
            page_items = data.get("setlist", []) if data else []
            if not page_items:
                break
            lineup.extend(page_items)
            if len(page_items) < 20:
                break
        if not lineup:
            return [item]

        show_key = self._show_key(item)
        matching = [candidate for candidate in lineup if self._show_key(candidate) == show_key]
        return matching or [item]

    def refresh_show_lineup(self, matched: MatchedConcert) -> MatchedConcert:
        """Refresh a selected show's lineup without carrying over another video's artists."""
        raw_item = dict(matched.raw_setlist_data or {})
        if not raw_item:
            return matched
        lineup = self._fetch_show_lineup(raw_item)
        if len(lineup) <= 1:
            return matched
        refreshed = self.parse_setlist_payload(
            raw_item,
            matched.event_date,
            all_setlists=lineup,
        )
        refreshed.source_url = matched.source_url or refreshed.source_url
        return refreshed

    def _fetch_setlists_raw(self, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
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
            return res.json()
        except Exception as e:
            logger.warning("Setlist.fm request failed: %s", e)
            return None

    def search_venues(
        self,
        name: str,
        city_name: Optional[str] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search Setlist.fm for venues matching name and optional city."""
        if not self.api_key or not name.strip():
            return []

        self._enforce_rate_limit()
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "StageSync/1.0",
        }
        url = f"{SETLIST_FM_API_BASE}/search/venues"
        params: Dict[str, str] = {"name": name.strip()}
        if city_name and city_name.strip():
            params["cityName"] = city_name.strip()

        try:
            res = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            if res.status_code == 404:
                return []
            res.raise_for_status()
            data = res.json()
            venues = data.get("venue", [])
            results = []
            for v in venues[:max_results]:
                city_obj = v.get("city", {})
                coords = city_obj.get("coords", {})
                results.append({
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "city": city_obj.get("name", "Unknown City"),
                    "state": city_obj.get("stateCode") or city_obj.get("state"),
                    "country": city_obj.get("country", {}).get("code", "US"),
                    "latitude": float(coords["lat"]) if coords.get("lat") is not None else None,
                    "longitude": float(coords["long"]) if coords.get("long") is not None else None,
                    "url": v.get("url"),
                })
            return results
        except Exception as e:
            logger.warning("Setlist.fm venue search failed: %s", e)
            return []

    def _query_setlists(
        self,
        params: Dict[str, str],
        fallback_date: str,
    ) -> Optional[MatchedConcert]:
        """Execute HTTP request against Setlist.fm search endpoint."""
        data = self._fetch_setlists_raw(params)
        if not data:
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

        return self.parse_setlist_payload(best, fallback_date, all_setlists=setlists)

    def parse_setlist_payload(
        self,
        item: Dict[str, Any],
        fallback_date: str,
        all_setlists: Optional[List[Dict[str, Any]]] = None,
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

        # Extract all lineup artists from all setlists on this bill
        lineup_artists: List[str] = []
        if all_setlists:
            for s in all_setlists:
                a_name = s.get("artist", {}).get("name")
                if a_name and a_name not in lineup_artists:
                    lineup_artists.append(a_name)
        if not lineup_artists and headliner:
            lineup_artists = [headliner]
        elif headliner and headliner not in lineup_artists:
            lineup_artists.insert(0, headliner)

        raw_setlist_data = dict(item)
        if all_setlists and len(all_setlists) > 1:
            raw_setlist_data["_stagesync_lineup_setlists"] = [
                {
                    "artist": entry.get("artist", {}).get("name"),
                    "songs": [
                        song.get("name")
                        for set_group in entry.get("sets", {}).get("set", [])
                        for song in set_group.get("song", [])
                        if song.get("name")
                    ],
                }
                for entry in all_setlists
                if entry.get("artist", {}).get("name")
            ]

        return MatchedConcert(
            setlist_fm_event_id=event_id,
            headliner_artist=headliner,
            lineup_artists=lineup_artists,
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
            raw_setlist_data=raw_setlist_data,
            source_url=source_url,
        )
