"""Test fixtures and mock payloads for StageSync test suite."""

import json
from typing import Any, Dict

SAMPLE_RED_ROCKS_PROBE_JSON: Dict[str, Any] = {
    "format": {
        "filename": "PXL_20240815_214201.mp4",
        "nb_streams": 2,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
        "duration": "84.250000",
        "size": "192937984",
        "bit_rate": "18320421",
        "tags": {
            "major_brand": "mp42",
            "minor_version": "0",
            "compatible_brands": "isommp42",
            "creation_time": "2024-08-16T03:42:01.000000Z",
            "com.apple.quicktime.creationdate": "2024-08-15T21:42:01-0600",
            "com.apple.quicktime.location.ISO6709": "+39.6653-105.2058+1960.0/",
            "com.apple.quicktime.make": "Google",
            "com.apple.quicktime.model": "Pixel 8 Pro",
        },
    },
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 3840,
            "height": 2160,
            "duration": "84.250000",
            "tags": {"creation_time": "2024-08-16T03:42:01.000000Z"},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "duration": "84.250000",
            "tags": {"creation_time": "2024-08-16T03:42:01.000000Z"},
        },
    ],
}

SAMPLE_SETLIST_FM_PAYLOAD: Dict[str, Any] = {
    "type": "setlists",
    "itemsPerPage": 20,
    "page": 1,
    "total": 1,
    "setlist": [
        {
            "id": "3bca982c",
            "versionId": "73e2a6d1",
            "eventDate": "15-08-2024",
            "lastUpdated": "2024-08-16T12:00:00Z",
            "artist": {
                "mbid": "0d35cb74-0f1e-450a-8bf8-d464293e507b",
                "name": "King Gizzard & The Lizard Wizard",
                "sortName": "King Gizzard & The Lizard Wizard",
                "disambiguation": "",
                "url": "https://www.setlist.fm/setlists/king-gizzard-and-the-lizard-wizard-3bca982c.html",
            },
            "venue": {
                "id": "6bd66a1a",
                "name": "Red Rocks Amphitheatre",
                "city": {
                    "id": "5431652",
                    "name": "Morrison",
                    "state": "Colorado",
                    "stateCode": "CO",
                    "country": {"code": "US", "name": "United States"},
                    "coords": {"lat": 39.6653, "long": -105.2058},
                },
                "url": "https://www.setlist.fm/venue/red-rocks-amphitheatre-morrison-co-usa-6bd66a1a.html",
            },
            "tour": {"name": "North America Tour 2024"},
            "sets": {
                "set": [
                    {
                        "song": [
                            {"name": "Daily Blues"},
                            {"name": "The Bitter Boogie"},
                            {"name": "Muddy Water"},
                            {"name": "Gila Monster"},
                            {"name": "Dragon"},
                            {"name": "Flamethrower"},
                        ]
                    },
                    {
                        "encore": 1,
                        "song": [
                            {"name": "Iron Lung"},
                            {"name": "Hypertension"},
                        ],
                    },
                ]
            },
            "url": "https://www.setlist.fm/setlist/king-gizzard-and-the-lizard-wizard/2024/red-rocks-amphitheatre-morrison-co-3bca982c.html",
        }
    ],
}

SAMPLE_NOMINATIM_PAYLOAD: Dict[str, Any] = {
    "place_id": 284729104,
    "osm_type": "way",
    "osm_id": 34918204,
    "lat": "39.6653",
    "lon": "-105.2058",
    "display_name": "Red Rocks Amphitheatre, 18300, West Alameda Parkway, Morrison, Jefferson County, Colorado, 80465, United States",
    "address": {
        "theatre": "Red Rocks Amphitheatre",
        "road": "West Alameda Parkway",
        "village": "Morrison",
        "county": "Jefferson County",
        "state": "Colorado",
        "ISO3166-2-lvl4": "US-CO",
        "postcode": "80465",
        "country": "United States",
        "country_code": "us",
    },
}

SAMPLE_SHAZAM_PAYLOAD: Dict[str, Any] = {
    "matches": [{"id": "67391024", "offset": 23.4, "channel": "0"}],
    "track": {
        "layout": "5",
        "type": "MUSIC",
        "key": "67391024",
        "title": "Dragon",
        "subtitle": "King Gizzard & The Lizard Wizard",
        "sections": [
            {
                "type": "SONG",
                "metapages": [],
                "metadata": [
                    {"title": "Album", "text": "PetroDragonic Apocalypse"},
                    {"title": "Label", "text": "KGLW"},
                    {"title": "Released", "text": "2023"},
                ],
            }
        ],
    },
}
