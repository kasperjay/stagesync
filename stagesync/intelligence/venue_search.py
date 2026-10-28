"""Multi-Provider Venue Search Engine.

Federates venue lookups across local SQLite database, Setlist.fm REST API v2,
and OpenStreetMap / Nominatim POI search. Deduplicates and enriches candidate
results to empower user-assisted concert re-identification.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from stagesync.database.connection import Database, get_db
from stagesync.intelligence.geocoder import ReverseGeocoder
from stagesync.intelligence.setlist_fm import SetlistFmClient

logger = logging.getLogger(__name__)


def _normalize_key(name: str, city: Optional[str] = None) -> str:
    """Normalize venue name and city into a canonical deduplication key."""
    combined = f"{name or ''} {city or ''}".lower()
    return re.sub(r"[^a-z0-9]", "", combined)


class VenueSearchEngine:
    """Federated venue search engine across local DB, Setlist.fm, and OSM."""

    def __init__(
        self,
        db: Optional[Database] = None,
        setlist_client: Optional[SetlistFmClient] = None,
        geocoder: Optional[ReverseGeocoder] = None,
    ) -> None:
        self.db = db or get_db()
        self.setlist_client = setlist_client or SetlistFmClient()
        self.geocoder = geocoder or ReverseGeocoder(db=self.db)

    def search(
        self,
        query: str,
        city: Optional[str] = None,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        """Federate venue search across local DB, Setlist.fm, and OpenStreetMap.

        Returns deduplicated candidate venues sorted with high-confidence sources first.
        """
        query_str = query.strip()
        if not query_str:
            return []

        city_str = city.strip() if city else None
        candidates: Dict[str, Dict[str, Any]] = {}

        # 1. Local Database Search
        try:
            local_venues = self.db.search_venues(query=query_str, city=city_str, limit=limit)
            for lv in local_venues:
                key = _normalize_key(lv.name, lv.city)
                candidates[key] = {
                    "id": lv.id,
                    "name": lv.name,
                    "city": lv.city or "Unknown City",
                    "state": lv.state_province,
                    "country": lv.country_code or "US",
                    "formatted_address": lv.formatted_address,
                    "latitude": lv.latitude,
                    "longitude": lv.longitude,
                    "osm_id": lv.osm_id,
                    "setlist_fm_venue_id": lv.setlist_fm_venue_id,
                    "sources": ["local"],
                }
        except Exception as e:
            logger.warning("Local DB venue search error: %s", e)

        # 2. Setlist.fm API Search (rich concert venue metadata)
        try:
            sfm_venues = self.setlist_client.search_venues(name=query_str, city_name=city_str, max_results=limit)
            for sv in sfm_venues:
                key = _normalize_key(sv.get("name", ""), sv.get("city", ""))
                if key in candidates:
                    cand = candidates[key]
                    if not cand.get("setlist_fm_venue_id") and sv.get("id"):
                        cand["setlist_fm_venue_id"] = sv["id"]
                    if cand.get("latitude") is None and sv.get("latitude") is not None:
                        cand["latitude"] = sv["latitude"]
                        cand["longitude"] = sv["longitude"]
                    if "setlist_fm" not in cand["sources"]:
                        cand["sources"].append("setlist_fm")
                else:
                    candidates[key] = {
                        "name": sv.get("name"),
                        "city": sv.get("city") or "Unknown City",
                        "state": sv.get("state"),
                        "country": sv.get("country", "US"),
                        "formatted_address": f"{sv.get('name')}, {sv.get('city')}, {sv.get('state') or sv.get('country')}",
                        "latitude": sv.get("latitude"),
                        "longitude": sv.get("longitude"),
                        "osm_id": None,
                        "setlist_fm_venue_id": sv.get("id"),
                        "sources": ["setlist_fm"],
                    }
        except Exception as e:
            logger.warning("Setlist.fm venue search error: %s", e)

        # 3. OpenStreetMap / Nominatim Search (POI and precise coordinates)
        try:
            osm_venues = self.geocoder.search_venues(query=query_str, city=city_str, limit=limit)
            for ov in osm_venues:
                key = _normalize_key(ov.get("name", ""), ov.get("city", ""))
                if key in candidates:
                    cand = candidates[key]
                    if not cand.get("osm_id") and ov.get("osm_id"):
                        cand["osm_id"] = ov["osm_id"]
                    if not cand.get("formatted_address") and ov.get("formatted_address"):
                        cand["formatted_address"] = ov["formatted_address"]
                    if cand.get("latitude") is None and ov.get("latitude") is not None:
                        cand["latitude"] = ov["latitude"]
                        cand["longitude"] = ov["longitude"]
                    if "osm" not in cand["sources"]:
                        cand["sources"].append("osm")
                else:
                    candidates[key] = {
                        "name": ov.get("name"),
                        "city": ov.get("city") or "Unknown City",
                        "state": ov.get("state"),
                        "country": ov.get("country", "US"),
                        "formatted_address": ov.get("formatted_address"),
                        "latitude": ov.get("latitude"),
                        "longitude": ov.get("longitude"),
                        "osm_id": ov.get("osm_id"),
                        "setlist_fm_venue_id": None,
                        "sources": ["osm"],
                    }
        except Exception as e:
            logger.warning("Nominatim venue search error: %s", e)

        # Sort: venues with local or setlist_fm IDs first, exact match boost
        results = list(candidates.values())

        def _sort_score(item: Dict[str, Any]) -> int:
            score = 0
            name_lower = (item.get("name") or "").lower()
            q_lower = query_str.lower()
            if name_lower == q_lower:
                score += 100
            elif name_lower.startswith(q_lower):
                score += 50
            if "local" in item.get("sources", []):
                score += 40
            if item.get("setlist_fm_venue_id"):
                score += 30
            if item.get("latitude") is not None:
                score += 10
            return -score

        results.sort(key=_sort_score)
        return results[:limit]
