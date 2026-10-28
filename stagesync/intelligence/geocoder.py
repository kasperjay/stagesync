"""Geospatial Reverse Geocoding with Local SQLite TTL Caching.

Queries OpenStreetMap / Nominatim API to resolve venue names, cities,
and administrative regions while adhering strictly to the 1 req/sec policy.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional
from pydantic import BaseModel
import requests

from stagesync.database.connection import Database, get_db

logger = logging.getLogger(__name__)

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "StageSync/1.0 (live-concert-video-archival; dev@stagesync.local)"


class ResolvedLocation(BaseModel):
    """Geospatial details resolved from coordinates."""

    display_name: str
    venue_name: Optional[str] = None
    city: str
    state_province: Optional[str] = None
    country_code: str
    latitude: float
    longitude: float
    osm_id: Optional[str] = None
    raw_payload: Dict[str, Any] = {}


class ReverseGeocoder:
    """Nominatim reverse geocoder with thread-safe rate-limiting and SQLite cache."""

    _last_request_time: float = 0.0
    _rate_limit_lock = threading.Lock()

    def __init__(self, db: Optional[Database] = None, timeout: float = 5.0) -> None:
        self.db = db or get_db()
        self.timeout = timeout

    @classmethod
    def _enforce_rate_limit(cls) -> None:
        """Enforce at least 1.0 second between consecutive Nominatim requests."""
        with cls._rate_limit_lock:
            elapsed = time.time() - cls._last_request_time
            if elapsed < 1.05:
                time.sleep(1.05 - elapsed)
            cls._last_request_time = time.time()

    def reverse_geocode(self, latitude: float, longitude: float) -> ResolvedLocation:
        """Resolve coordinates to venue, city, and country.

        Uses local cache rounded to 3 decimal places (~110m).
        """
        # Round to 3 decimal places for caching
        lat_rounded = round(latitude, 3)
        lon_rounded = round(longitude, 3)
        cache_key = f"nom_{lat_rounded:.3f}_{lon_rounded:.3f}"

        # 1. Check SQLite Cache
        cached = self.db.get_cached_geocode(cache_key)
        if cached:
            logger.debug("Geocode cache hit for key: %s", cache_key)
            return ResolvedLocation(
                display_name=cached["display_name"] or "",
                venue_name=cached["venue_name"],
                city=cached["city"] or "Unknown City",
                state_province=cached["state_province"],
                country_code=cached["country_code"] or "US",
                latitude=latitude,
                longitude=longitude,
                osm_id=cached["osm_id"],
                raw_payload=cached.get("raw", {}),
            )

        # 2. Query Nominatim API with rate limiting
        self._enforce_rate_limit()

        params = {
            "format": "jsonv2",
            "lat": f"{latitude:.6f}",
            "lon": f"{longitude:.6f}",
            "zoom": "18",
            "addressdetails": "1",
        }
        headers = {"User-Agent": USER_AGENT}

        try:
            res = requests.get(
                NOMINATIM_REVERSE_URL,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            logger.warning("Nominatim request failed for (%s, %s): %s", latitude, longitude, e)
            # Return graceful fallback without crashing
            return ResolvedLocation(
                display_name=f"Location ({latitude:.4f}, {longitude:.4f})",
                venue_name=None,
                city="Unknown City",
                country_code="US",
                latitude=latitude,
                longitude=longitude,
                raw_payload={},
            )

        # 3. Parse Nominatim Response
        address = data.get("address", {})
        display_name = data.get("display_name", "")

        # Detect venue / POI name
        venue_name = None
        poi_keys = [
            "amenity",
            "leisure",
            "theatre",
            "stadium",
            "historic",
            "building",
            "tourism",
            "arts_centre",
            "club",
        ]
        for k in poi_keys:
            if k in address:
                venue_name = address[k]
                break

        if not venue_name and data.get("name"):
            venue_name = data.get("name")

        # Resolve City
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
            or "Unknown City"
        )
        state_province = address.get("state") or address.get("region")
        country_code = address.get("country_code", "US").upper()
        osm_id = str(data.get("osm_id", ""))

        resolved = ResolvedLocation(
            display_name=display_name,
            venue_name=venue_name,
            city=city,
            state_province=state_province,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            osm_id=osm_id,
            raw_payload=data,
        )

        # 4. Save into SQLite Cache
        cache_data = {
            "display_name": display_name,
            "venue_name": venue_name,
            "city": city,
            "state_province": state_province,
            "country_code": country_code,
            "latitude": latitude,
            "longitude": longitude,
            "osm_id": osm_id,
            "raw": data,
        }
        self.db.set_cached_geocode(cache_key, cache_data, ttl_days=30)

        return resolved

    def search_venues(
        self,
        query: str,
        city: Optional[str] = None,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """Search OpenStreetMap / Nominatim for venues matching query and optional city."""
        if not query.strip():
            return []

        self._enforce_rate_limit()
        full_query = f"{query.strip()}, {city.strip()}" if city and city.strip() else query.strip()
        params = {
            "format": "jsonv2",
            "q": full_query,
            "addressdetails": "1",
            "limit": str(limit),
        }
        headers = {"User-Agent": USER_AGENT}

        try:
            res = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            res.raise_for_status()
            items = res.json()
            results = []
            for item in items:
                address = item.get("address", {})
                v_name = item.get("name")
                if not v_name:
                    poi_keys = [
                        "amenity", "leisure", "theatre", "stadium", "historic",
                        "building", "tourism", "arts_centre", "club"
                    ]
                    for k in poi_keys:
                        if k in address:
                            v_name = address[k]
                            break
                if not v_name:
                    v_name = item.get("display_name", "").split(",")[0].strip()

                c_name = (
                    address.get("city")
                    or address.get("town")
                    or address.get("village")
                    or address.get("municipality")
                    or address.get("county")
                    or "Unknown City"
                )
                results.append({
                    "name": v_name,
                    "city": c_name,
                    "state": address.get("state") or address.get("region"),
                    "country": address.get("country_code", "US").upper(),
                    "formatted_address": item.get("display_name"),
                    "latitude": float(item["lat"]) if "lat" in item else None,
                    "longitude": float(item["lon"]) if "lon" in item else None,
                    "osm_id": str(item.get("osm_id", "")),
                })
            return results
        except Exception as e:
            logger.warning("Nominatim forward search failed for %s: %s", full_query, e)
            return []

